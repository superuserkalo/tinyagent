const std = @import("std");
const core = @import("agent.zig");
const Session = @import("session.zig").Session;
const Event = @import("types.zig").Event;
const output = @import("rpc_output.zig");
const Io = std.Io;
const Allocator = std.mem.Allocator;

pub const Options = struct {
    cancellation: ?*Io.Event = null,
};

/// Serve one session over native UTF-8 JSONL files. The caller owns both files
/// and consumes output concurrently. This function does not close the files,
/// change descriptor flags, or install process signal handlers. Use an Io with
/// cancellable native file I/O (such as Io.Threaded) and a thread-safe allocator.
/// EOF, cancellation, and transport failure join all cooperative run cleanup.
pub fn run(allocator: Allocator, io: Io, agent: core.Agent, input: Io.File, destination: Io.File, options: Options) !void {
    var session = Session.init(allocator, io, agent);
    defer session.deinit();
    var writer: output.Output = .{ .allocator = allocator, .io = io };
    var worker = try io.concurrent(output.Output.work, .{ &writer, destination });
    var closed = false;
    defer if (!closed) writer.close(&worker) catch {};
    var server: Server = .{ .allocator = allocator, .io = io, .session = &session, .writer = &writer };
    const Outcome = union(enum) { input: anyerror!void, failed: Io.Cancelable!void, cancelled: Io.Cancelable!void };
    var outcomes: [3]Outcome = undefined;
    var select: Io.Select(Outcome) = .init(io, &outcomes);
    defer select.cancelDiscard();
    try select.concurrent(.input, Server.read, .{ &server, input });
    try select.concurrent(.failed, Io.Event.wait, .{ &writer.failed, io });
    if (options.cancellation) |signal| try select.concurrent(.cancelled, Io.Event.wait, .{ signal, io });
    const outcome = select.await() catch Outcome{ .cancelled = error.Canceled };
    _ = session.stop();
    select.cancelDiscard();
    closed = true;
    try writer.close(&worker);
    if (outcome == .input) outcome.input catch |err| switch (err) {
        error.Canceled => {},
        else => return err,
    };
}

const Server = struct {
    allocator: Allocator,
    io: Io,
    session: *Session,
    writer: *output.Output,
    job: ?*Job = null,

    fn read(self: *Server, input: Io.File) !void {
        defer {
            _ = self.session.stop();
            self.join();
        }
        var partial: std.ArrayList(u8) = .empty;
        defer partial.deinit(self.allocator);
        var buffer: [8192]u8 = undefined;
        while (true) {
            const count = input.readStreaming(self.io, &.{&buffer}) catch |err| switch (err) {
                error.EndOfStream => {
                    if (partial.items.len != 0) _ = try self.line(partial.items);
                    return;
                },
                else => return err,
            };
            var remaining = buffer[0..count];
            while (remaining.len != 0) {
                const newline = std.mem.indexOfScalar(u8, remaining, '\n');
                const length = newline orelse remaining.len;
                if (partial.items.len + length > output.max_line_bytes) {
                    try self.protocolError("line_too_large", "RPC input exceeds 1048576 UTF-8 bytes");
                    return;
                }
                try partial.appendSlice(self.allocator, remaining[0..length]);
                remaining = remaining[length + @intFromBool(newline != null) ..];
                if (newline != null) {
                    if (!try self.line(partial.items)) return;
                    partial.clearRetainingCapacity();
                }
            }
        }
    }

    fn line(self: *Server, bytes: []const u8) !bool {
        if (!std.unicode.utf8ValidateSlice(bytes)) {
            try self.protocolError("invalid_utf8", "RPC input is not valid UTF-8");
            return false;
        }
        const parsed = std.json.parseFromSlice(std.json.Value, self.allocator, bytes, .{ .allocate = .alloc_always }) catch |err| {
            if (err == error.OutOfMemory) return err;
            try self.protocolError("invalid_json", "RPC input is not valid JSON");
            return true;
        };
        defer parsed.deinit();
        if (parsed.value != .object) {
            try self.protocolError("invalid_command", "RPC command must be an object");
            return true;
        }
        const object = parsed.value.object;
        const id: ?[]const u8 = if (object.get("id")) |value| blk: {
            if (value != .string) {
                try self.protocolError("invalid_command", "command.id must be a string");
                return true;
            }
            break :blk value.string;
        } else null;
        const kind = string(object.get("type")) orelse "unknown";
        if (std.mem.eql(u8, kind, "prompt")) {
            const message = string(object.get("message"));
            if (message == null or message.?.len == 0) {
                try self.commandError(id, kind, "invalid_command", "prompt.message must be a non-empty string");
            } else if (self.session.state().run_id != null) {
                try self.commandError(id, kind, "busy", "A run is already active");
            } else {
                self.join();
                const job = try Job.start(self, message.?, id);
                self.job = job;
                // before_run signals after reservation, before waiting on the
                // writer. Early admission failures signal from Job.run's defer.
                try job.admitted.wait(self.io);
            }
        } else if (std.mem.eql(u8, kind, "abort")) {
            const run_id = self.session.cancel();
            try self.reply(id, kind, .{ .accepted = run_id != null, .runId = NullableString{ .value = if (run_id) |*value| value else null } });
        } else if (std.mem.eql(u8, kind, "new_session")) {
            const aborted = try self.session.reset();
            self.join();
            try self.reply(id, kind, .{ .abortedRun = aborted != null });
        } else if (std.mem.eql(u8, kind, "get_state")) {
            const state = self.session.state();
            try self.reply(id, kind, .{ .protocolVersion = 1, .status = @as([]const u8, if (state.run_id != null) "running" else "idle"), .runId = NullableString{ .value = if (state.run_id) |*value| value else null }, .messageCount = state.message_count });
        } else {
            try self.commandError(id, kind, "invalid_command", "Unknown command type");
        }
        return true;
    }

    fn join(self: *Server) void {
        if (self.job) |job| {
            job.future.?.await(self.io);
            self.allocator.free(job.prompt);
            if (job.id) |id| self.allocator.free(id);
            self.allocator.destroy(job);
            self.job = null;
        }
    }

    fn enqueue(self: *Server, value: anytype) !*output.Frame {
        const frame = try self.writer.enqueue(value);
        if (frame.oversized) _ = self.session.cancel();
        return frame;
    }

    fn reply(self: *Server, id: ?[]const u8, kind: []const u8, data: anytype) !void {
        const frame = try self.enqueue(.{ .id = id, .type = "response", .command = kind, .success = true, .data = data });
        frame.release();
    }

    fn protocolError(self: *Server, code: []const u8, message: []const u8) !void {
        const frame = try self.enqueue(.{ .type = "protocol_error", .@"error" = .{ .code = code, .message = message } });
        frame.release();
    }

    fn commandError(self: *Server, id: ?[]const u8, kind: []const u8, code: []const u8, message: []const u8) !void {
        if (id == null) return self.protocolError(code, message);
        const frame = try self.enqueue(.{ .id = id, .type = "response", .command = kind, .success = false, .@"error" = .{ .code = code, .message = message } });
        frame.release();
    }
};

const Job = struct {
    server: *Server,
    prompt: []u8,
    id: ?[]u8,
    future: ?Io.Future(void) = null,
    admitted: Io.Event = .unset,

    fn start(server: *Server, message: []const u8, id: ?[]const u8) !*Job {
        const job = try server.allocator.create(Job);
        errdefer server.allocator.destroy(job);
        const prompt = try server.allocator.dupe(u8, message);
        errdefer server.allocator.free(prompt);
        const command_id = if (id) |value| try server.allocator.dupe(u8, value) else null;
        errdefer if (command_id) |value| server.allocator.free(value);
        job.* = .{ .server = server, .prompt = prompt, .id = command_id };
        job.future = try server.io.concurrent(Job.run, .{job});
        return job;
    }

    fn run(self: *Job) void {
        defer self.admitted.set(self.server.io);
        var result = self.server.session.run(self.prompt, .{
            .before_run = .{ .context = self, .callback = acknowledge },
            .on_event = .{ .context = self, .callback = emit },
        }) catch |err| {
            if (!self.admitted.isSet()) self.server.commandError(self.id, "prompt", "run_failed", @errorName(err)) catch |failure| self.server.writer.fail(failure);
            return;
        };
        result.deinit();
    }

    fn acknowledge(context: ?*anyopaque, io: Io, id: []const u8) !void {
        const self: *Job = @ptrCast(@alignCast(context.?));
        self.admitted.set(io);
        const frame = try self.server.enqueue(.{ .id = self.id, .type = "response", .command = "prompt", .success = true, .data = .{ .runId = id } });
        try self.server.writer.wait(frame, false);
    }

    fn emit(context: ?*anyopaque, _: Io, event: Event) !void {
        const self: *Job = @ptrCast(@alignCast(context.?));
        const frame = try self.server.enqueue(event);
        try self.server.writer.wait(frame, event.kind == .aborted);
    }
};

fn string(value: ?std.json.Value) ?[]const u8 {
    const item = value orelse return null;
    return if (item == .string) item.string else null;
}

// Optional command IDs are omitted; the protocol requires null run IDs.
const NullableString = struct {
    value: ?[]const u8,

    pub fn jsonStringify(self: NullableString, json: *std.json.Stringify) !void {
        try json.write(self.value);
    }
};
