const std = @import("std");
const sdk = @import("tinyagent");
const testing = std.testing;
const allocator = testing.allocator;
const io = testing.io;

const Fixture = struct {
    provider_calls: usize = 0,
    handler_calls: usize = 0,
    waiting: bool = false,
    entered: std.Io.Event = .unset,
    cleaned: bool = false,

    fn handle(context: ?*anyopaque, memory: std.mem.Allocator, task_io: std.Io, arguments: std.json.Value) ![]const u8 {
        const self: *Fixture = @ptrCast(@alignCast(context.?));
        self.handler_calls += 1;
        defer self.cleaned = true;
        if (self.waiting) {
            self.entered.set(task_io);
            var pending: std.Io.Event = .unset;
            try pending.wait(task_io);
            return error.UnexpectedWakeup;
        }
        const text = arguments.object.get("text").?.string;
        if (std.mem.eql(u8, text, "fail")) return error.ToolUnavailable;
        if (std.mem.eql(u8, text, "big")) return memory.alloc(u8, 131_073);
        return text;
    }

    fn stream(context: ?*anyopaque, memory: std.mem.Allocator, task_io: std.Io, request: sdk.ProviderRequest, text: sdk.TextHandler) !sdk.ProviderTurn {
        const self: *Fixture = @ptrCast(@alignCast(context.?));
        self.provider_calls += 1;
        try testing.expectEqual(1, request.tools.len);
        try testing.expectEqualStrings("object", request.tools[0].parameters.object.get("type").?.string);
        if (self.provider_calls == 2) {
            try testing.expectEqual(4, self.handler_calls);
            try testing.expectEqual(7, request.history.len);
            for ([_][]const u8{ "one", "ToolUnavailable", "InvalidToolArguments", "ToolOutputTooLarge", "two" }, 2..) |expected, index| {
                const observation = request.history[index].tool_result;
                try testing.expectEqualStrings(expected, observation.output);
                try testing.expectEqual(index != 2 and index != 6, observation.is_error);
            }
            try text.emit(task_io, "recovered");
            return .{ .assistant = .{ .text = "recovered" } };
        }
        try testing.expectEqual(1, self.provider_calls);
        // A mutable provider request must not rewrite the Tool's checked schema.
        request.tools[0].parameters.object.getPtr("type").?.* = .{ .string = "number" };
        if (!self.waiting) try text.emit(task_io, "thinking");
        const payloads = [_][]const u8{ "{\"text\":\"one\"}", "{\"text\":\"fail\"}", "{\"text\":42}", "{\"text\":\"big\"}", "{\"text\":\"two\"}" };
        const calls = try memory.alloc(sdk.ToolCall, if (self.waiting) 1 else payloads.len);
        for (calls, 0..) |*call, index| {
            const parsed = try std.json.parseFromSlice(std.json.Value, memory, payloads[index], .{});
            call.* = .{ .call_id = try std.fmt.allocPrint(memory, "c_{d}", .{index}), .name = "work", .arguments = parsed.value };
        }
        return .{ .assistant = .{ .text = "thinking", .tool_calls = calls } };
    }

    fn tool(self: *Fixture) !sdk.Tool {
        const parsed = try std.json.parseFromSlice(std.json.Value, allocator,
            \\{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}
        , .{});
        defer parsed.deinit();
        return sdk.Tool.init(allocator, .{ .name = "work", .description = "Work", .parameters = parsed.value }, .{ .context = self, .callback = handle });
    }

    fn agent(self: *Fixture, tools: []const *const sdk.Tool) sdk.Agent {
        return .{ .model = "offline", .provider = .{ .context = self, .stream = stream }, .tools = tools };
    }
};

const Recorder = struct {
    lines: std.ArrayList([]const u8) = .empty,
    fail_at: ?u64 = null,
    cancel_on_start: ?*std.Io.Event = null,

    fn handler(self: *Recorder) sdk.EventHandler {
        return .{ .context = self, .callback = record };
    }

    fn record(context: ?*anyopaque, task_io: std.Io, event: sdk.Event) !void {
        const self: *Recorder = @ptrCast(@alignCast(context.?));
        try testing.expectEqual(self.lines.items.len + 1, event.sequence);
        const line = try std.json.Stringify.valueAlloc(allocator, event, .{});
        self.lines.append(allocator, line) catch |err| {
            allocator.free(line);
            return err;
        };
        if (event.kind == .tool_execution_start) {
            if (self.cancel_on_start) |signal| signal.set(task_io);
        }
        if (self.fail_at == event.sequence) return error.ConsumerStopped;
    }

    fn deinit(self: *Recorder) void {
        for (self.lines.items) |line| allocator.free(line);
        self.lines.deinit(allocator);
    }
};

test "one agent loop executes sequential tools and recovers from bounded observations" {
    var fixture: Fixture = .{};
    var tool = try fixture.tool();
    defer tool.deinit();
    var recorder: Recorder = .{};
    defer recorder.deinit();
    var result = try fixture.agent(&.{&tool}).run(allocator, io, "go", .{ .on_event = recorder.handler(), .run_id = "r_tools" });
    defer result.deinit();
    try testing.expectEqualStrings("recovered", result.text);
    try testing.expectEqual(8, result.history.len);
    try testing.expectEqual(14, recorder.lines.items.len);
    for (0..5) |index| {
        const start = try std.json.parseFromSlice(std.json.Value, allocator, recorder.lines.items[2 + index * 2], .{});
        defer start.deinit();
        const end = try std.json.parseFromSlice(std.json.Value, allocator, recorder.lines.items[3 + index * 2], .{});
        defer end.deinit();
        try testing.expectEqualStrings("tool_execution_start", start.value.object.get("type").?.string);
        try testing.expectEqualStrings("tool_execution_end", end.value.object.get("type").?.string);
        try testing.expectEqualStrings(start.value.object.get("toolCallId").?.string, end.value.object.get("toolCallId").?.string);
        try testing.expectEqualStrings("r_tools", end.value.object.get("runId").?.string);
        try testing.expectEqualStrings("work", end.value.object.get("toolName").?.string);
    }
}

test "tool start and handler cancellation both pair the end before aborting" {
    for ([_]bool{ true, false }) |cancel_on_start| {
        var fixture: Fixture = .{ .waiting = true };
        var tool = try fixture.tool();
        defer tool.deinit();
        var signal: std.Io.Event = .unset;
        var recorder: Recorder = .{ .cancel_on_start = if (cancel_on_start) &signal else null };
        defer recorder.deinit();
        const configured = fixture.agent(&.{&tool});
        var future = try io.concurrent(sdk.Agent.run, .{ configured, allocator, io, "go", sdk.RunOptions{ .on_event = recorder.handler(), .cancellation = &signal } });
        defer if (future.cancel(io)) |value| {
            var discarded = value;
            discarded.deinit();
        } else |_| {};
        if (!cancel_on_start) {
            try fixture.entered.wait(io);
            signal.set(io);
        }
        try testing.expectError(error.Canceled, future.await(io));
        try testing.expectEqual(if (cancel_on_start) @as(usize, 0) else 1, fixture.handler_calls);
        try testing.expectEqual(!cancel_on_start, fixture.cleaned);
        try testing.expectEqual(4, recorder.lines.items.len);
        const end = try std.json.parseFromSlice(std.json.Value, allocator, recorder.lines.items[2], .{});
        defer end.deinit();
        try testing.expectEqualStrings("tool_execution_end", end.value.object.get("type").?.string);
        try testing.expect(end.value.object.get("isError").?.bool);
        const terminal = try std.json.parseFromSlice(std.json.Value, allocator, recorder.lines.items[3], .{});
        defer terminal.deinit();
        try testing.expectEqualStrings("aborted", terminal.value.object.get("status").?.string);
    }
}

test "failed tool event delivery stops handlers and preserves the callback error" {
    for ([_]u64{ 3, 4, 14 }) |sequence| {
        var fixture: Fixture = .{};
        var tool = try fixture.tool();
        defer tool.deinit();
        var recorder: Recorder = .{ .fail_at = sequence };
        defer recorder.deinit();
        try testing.expectError(error.ConsumerStopped, fixture.agent(&.{&tool}).run(allocator, io, "go", .{ .on_event = recorder.handler() }));
        try testing.expectEqual(sequence, recorder.lines.items.len);
        try testing.expectEqual(switch (sequence) {
            3 => @as(usize, 0),
            4 => 1,
            14 => 4,
            else => unreachable,
        }, fixture.handler_calls);
    }
}

test "duplicate tool names fail before callbacks or provider work" {
    var fixture: Fixture = .{};
    var tool = try fixture.tool();
    defer tool.deinit();
    var recorder: Recorder = .{};
    defer recorder.deinit();
    try testing.expectError(error.DuplicateToolName, fixture.agent(&.{ &tool, &tool }).run(allocator, io, "go", .{ .on_event = recorder.handler() }));
    try testing.expectEqual(0, recorder.lines.items.len);
    try testing.expectEqual(0, fixture.provider_calls);
}
