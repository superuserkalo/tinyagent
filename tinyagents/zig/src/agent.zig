const std = @import("std");
const types = @import("types.zig");
const history = @import("history.zig");
const Tool = @import("tool.zig").Tool;
const Allocator = std.mem.Allocator;
const Io = std.Io;

pub const RunId = [34]u8;

pub fn newRunId(io: Io) !RunId {
    var random: [16]u8 = undefined;
    try io.randomSecure(&random);
    var id: RunId = undefined;
    _ = try std.fmt.bufPrint(&id, "r_{x}", .{&random});
    return id;
}

pub const RunOptions = struct {
    history: []const types.HistoryMessage = &.{},
    run_id: ?[]const u8 = null,
    on_event: ?types.EventHandler = null,
    /// Set this event to request cancellation, including from a callback.
    /// Agent.run interrupts native I/O and joins cleanup before returning.
    cancellation: ?*Io.Event = null,
};

/// Owns every returned string, message, and JSON value. Do not copy ownership;
/// call deinit once. A new result never borrows from an earlier run or provider.
pub const RunResult = struct {
    arena: *std.heap.ArenaAllocator,
    run_id: []const u8,
    text: []const u8,
    history: []const types.HistoryMessage,
    output: []const std.json.Value,

    pub fn deinit(self: *RunResult) void {
        const allocator = self.arena.child_allocator;
        self.arena.deinit();
        allocator.destroy(self.arena);
        self.* = undefined;
    }
};

/// Immutable borrowed configuration. Keep its strings and provider context
/// alive for every run. Each call owns its history and event sequence.
pub const Agent = struct {
    provider: types.Provider,
    model: []const u8,
    instructions: []const u8 = "",
    tools: []const *const Tool = &.{},

    /// Use the caller's Io, with an owned task when a cancellation event is
    /// supplied. Alternatively, use io.concurrent and cancel its returned Future.
    /// Callback errors take precedence over provider errors, even if a provider
    /// incorrectly catches them. A failed terminal delivery never returns history.
    pub fn run(self: Agent, allocator: Allocator, io: Io, prompt: []const u8, options: RunOptions) anyerror!RunResult {
        if (options.cancellation) |signal| {
            // Establish identity before entering the cancellable worker, so a
            // previously set signal still receives ordered start/aborted events.
            var id: RunId = undefined;
            var configured = options;
            if (configured.run_id == null) {
                id = try newRunId(io);
                configured.run_id = &id;
            }
            return runControlled(io, signal, Agent.runInner, .{ self, allocator, io, prompt, configured });
        }
        return self.runInner(allocator, io, prompt, options);
    }

    fn runInner(self: Agent, allocator: Allocator, io: Io, prompt: []const u8, options: RunOptions) anyerror!RunResult {
        if (self.model.len == 0) return error.EmptyModel;
        if (!std.unicode.utf8ValidateSlice(self.model) or
            !std.unicode.utf8ValidateSlice(self.instructions)) return error.InvalidUtf8;
        for (self.tools, 0..) |tool, index| {
            for (self.tools[0..index]) |previous| {
                if (std.mem.eql(u8, tool.metadata.name, previous.metadata.name)) return error.DuplicateToolName;
            }
        }
        const arena = try allocator.create(std.heap.ArenaAllocator);
        arena.* = .init(allocator);
        errdefer {
            arena.deinit();
            allocator.destroy(arena);
        }
        const memory = arena.allocator();
        const id = if (options.run_id) |value|
            try history.identifier(memory, value)
        else blk: {
            const generated = try newRunId(io);
            break :blk try memory.dupe(u8, &generated);
        };
        var transcript: std.ArrayList(types.HistoryMessage) = .empty;
        try transcript.ensureTotalCapacity(memory, options.history.len + 2);
        for (options.history) |item| {
            transcript.appendAssumeCapacity(try history.message(memory, item));
        }
        transcript.appendAssumeCapacity(.{ .user = try history.text(memory, prompt) });
        var events: RunEvents = .{ .id = id, .handler = options.on_event, .cancellation = options.cancellation };
        const result = self.perform(memory, io, &transcript, &events) catch |err| {
            if (events.failure) |failure| return failure;
            // Consume pending cancellation before choosing a terminal status.
            const failure = if (events.checkCancel(io)) |_| events.stream_failure orelse err else |_| error.Canceled;
            try events.emit(io, if (failure == error.Canceled) .aborted else .{ .failed = failure });
            return failure;
        };
        events.checkCancel(io) catch |err| {
            try events.emit(io, .aborted);
            return err;
        };
        try events.emit(io, .{ .completed = result.assistant.text });
        try events.checkCancel(io);
        return .{
            .arena = arena,
            .run_id = id,
            .text = result.assistant.text,
            .history = transcript.items,
            .output = result.output,
        };
    }

    fn perform(self: Agent, allocator: Allocator, io: Io, transcript: *std.ArrayList(types.HistoryMessage), events: *RunEvents) !types.ProviderTurn {
        try events.emit(io, .agent_start);
        while (true) {
            try events.checkCancel(io);
            const definitions = try allocator.alloc(types.ToolDefinition, self.tools.len);
            for (self.tools, definitions) |tool, *definition| {
                definition.* = tool.definition();
                definition.parameters = try history.cloneJson(allocator, definition.parameters);
            }
            const turn = try self.provider.stream(self.provider.context, allocator, io, .{
                .model = self.model,
                .instructions = self.instructions,
                .history = transcript.items,
                .tools = definitions,
            }, .{ .context = events, .callback = RunEvents.onText });
            if (events.failure) |failure| return failure;
            if (events.stream_failure) |failure| return failure;
            try events.checkCancel(io);
            const assistant = try history.assistant(allocator, turn.assistant);
            try transcript.append(allocator, .{ .assistant = assistant });
            if (assistant.tool_calls.len == 0) {
                return .{ .assistant = assistant, .output = try history.values(allocator, turn.output) };
            }
            for (assistant.tool_calls) |call| {
                try events.checkCancel(io);
                const tool = for (self.tools) |candidate| {
                    if (std.mem.eql(u8, call.name, candidate.metadata.name)) break candidate;
                } else return error.UnregisteredTool;
                const observation = try executeTool(tool.*, allocator, io, call, events);
                try transcript.append(allocator, .{ .tool_result = observation });
            }
        }
    }
};

fn executeTool(tool: Tool, allocator: Allocator, io: Io, call: types.ToolCall, events: *RunEvents) !types.ToolResultMessage {
    try events.emit(io, .{ .tool_execution_start = call });
    var failure: ?anyerror = null;
    // A start callback may cancel before the handler begins. It still receives
    // an end event, just like a handler interrupted during native I/O.
    const execution: anyerror![]const u8 = if (events.checkCancel(io)) |_|
        tool.execute(allocator, io, call.arguments)
    else |err|
        err;
    const output = execution catch |err| blk: {
        failure = err;
        break :blk @errorName(err);
    };
    events.checkCancel(io) catch {
        failure = error.Canceled;
    };
    const result: types.ToolResultMessage = .{
        .call_id = call.call_id,
        .name = call.name,
        .output = if (failure != null and failure.? == error.Canceled) "Tool cancelled" else output,
        .is_error = failure != null,
    };
    try events.emit(io, .{ .tool_execution_end = result });
    if (failure) |err| switch (err) {
        error.Canceled, error.OutOfMemory, error.SchemaValidatorFailed => return err,
        else => {},
    };
    return result;
}

const RunEvents = struct {
    id: []const u8,
    handler: ?types.EventHandler,
    sequence: u64 = 0,
    failure: ?anyerror = null,
    stream_failure: ?anyerror = null,
    cancellation: ?*Io.Event = null,

    fn checkCancel(self: *RunEvents, io: Io) !void {
        try io.checkCancel();
        if (self.cancellation) |signal| {
            if (signal.isSet()) return error.Canceled;
        }
    }

    fn emit(self: *RunEvents, io: Io, kind: @FieldType(types.Event, "kind")) anyerror!void {
        if (self.failure) |failure| return failure;
        self.sequence += 1;
        if (self.handler) |handler| {
            handler.emit(io, .{ .run_id = self.id, .sequence = self.sequence, .kind = kind }) catch |err| {
                self.failure = err;
                return err;
            };
        }
    }

    fn onText(context: ?*anyopaque, io: Io, text: []const u8) !void {
        const self: *RunEvents = @ptrCast(@alignCast(context.?));
        if (self.stream_failure) |failure| return failure;
        self.forwardText(io, text) catch |err| {
            self.stream_failure = err;
            return err;
        };
    }

    fn forwardText(self: *RunEvents, io: Io, text: []const u8) !void {
        if (self.failure) |failure| return failure;
        try self.checkCancel(io);
        if (!std.unicode.utf8ValidateSlice(text)) return error.InvalidUtf8;
        if (text.len != 0) try self.emit(io, .{ .text_delta = text });
        try self.checkCancel(io);
    }
};

// Package-internal entrypoint for a session whose acknowledgement and agent
// work already share an owned cancellation scope.
pub fn runInScope(agent: Agent, allocator: Allocator, io: Io, prompt: []const u8, options: RunOptions) anyerror!RunResult {
    return agent.runInner(allocator, io, prompt, options);
}

// Own one worker and one cancellation waiter. Both finish before any borrowed
// state leaves scope. Resource-bearing results are freed on every failed exit.
pub fn runControlled(io: Io, signal: *Io.Event, comptime function: anytype, args: std.meta.ArgsTuple(@TypeOf(function))) anyerror!RunResult {
    const Work = struct {
        result: ?anyerror!RunResult = null,

        fn perform(self: *@This(), arguments: @TypeOf(args)) void {
            self.result = @call(.auto, function, arguments);
        }
    };
    const Outcome = union(enum) { work: void, cancelled: Io.Cancelable!void };
    var work: Work = .{};
    var outcomes: [2]Outcome = undefined;
    var select: Io.Select(Outcome) = .init(io, &outcomes);
    var transferred = false;
    defer {
        select.cancelDiscard();
        if (!transferred) {
            if (work.result) |outcome| {
                if (outcome) |value| {
                    var discarded = value;
                    discarded.deinit();
                } else |_| {}
            }
        }
    }
    try select.concurrent(.work, Work.perform, .{ &work, args });
    try select.concurrent(.cancelled, Io.Event.wait, .{ signal, io });
    const outcome = select.await() catch Outcome{ .cancelled = error.Canceled };
    var cancelled = outcome == .cancelled;
    select.cancelDiscard();
    io.checkCancel() catch {
        cancelled = true;
    };
    const result = try work.result.?;
    if (cancelled or signal.isSet()) return error.Canceled;
    transferred = true;
    return result;
}
