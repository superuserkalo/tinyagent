const std = @import("std");
const sdk = @import("tinyagent");
const testing = std.testing;
const allocator = testing.allocator;
const io = testing.io;

const Handler = struct {
    calls: usize = 0,
    mode: enum { echo, fail, oversized, invalid, mutate } = .echo,

    fn execute(context: ?*anyopaque, memory: std.mem.Allocator, _: std.Io, arguments: std.json.Value) ![]const u8 {
        const self: *Handler = @ptrCast(@alignCast(context.?));
        self.calls += 1;
        return switch (self.mode) {
            .echo => "ok",
            .fail => error.ToolUnavailable,
            .oversized => try memory.alloc(u8, 131_073),
            .invalid => "\xff",
            .mutate => blk: {
                const text = arguments.object.get("text").?.string;
                @constCast(text)[0] = 'H';
                break :blk text;
            },
        };
    }

    fn handler(self: *Handler) sdk.ToolHandler {
        return .{ .context = self, .callback = execute };
    }
};

test "shared argument cases validate before native handlers execute" {
    const fixture = try std.json.parseFromSlice(std.json.Value, allocator, @embedFile("fixtures/tool_arguments.json"), .{ .parse_numbers = false });
    defer fixture.deinit();
    for (fixture.value.object.get("cases").?.array.items) |scenario| {
        var handler: Handler = .{};
        var tool = try sdk.Tool.init(allocator, .{
            .name = "check",
            .description = "Check",
            .parameters = scenario.object.get("schema").?,
        }, handler.handler());
        defer tool.deinit();
        var run = std.heap.ArenaAllocator.init(allocator);
        defer run.deinit();
        const valid = scenario.object.get("valid").?.array.items;
        for (valid) |arguments| {
            try testing.expectEqualStrings("ok", try tool.execute(run.allocator(), io, arguments));
        }
        for (scenario.object.get("invalid").?.array.items) |arguments| {
            try testing.expectError(error.InvalidToolArguments, tool.execute(run.allocator(), io, arguments));
        }
        try testing.expectEqual(valid.len, handler.calls);
    }
}

test "invalid schemas and unresolved references fail at construction" {
    var handler: Handler = .{};
    for ([_][]const u8{
        "true",
        "{\"type\":\"nonsense\"}",
        "{\"minimum\":\"wrong\"}",
        "{\"minLength\":-1}",
        "{\"required\":[\"x\",\"x\"]}",
        "{\"$async\":true}",
        "{\"$ref\":\"https://example.invalid/schema\"}",
        "{\"properties\":{\"x\":{\"$ref\":\"#/$defs/missing\"}}}",
    }) |source| {
        const parsed = try std.json.parseFromSlice(std.json.Value, allocator, source, .{});
        defer parsed.deinit();
        try testing.expectError(error.InvalidToolSchema, sdk.Tool.init(allocator, .{
            .name = "check",
            .description = "Check",
            .parameters = parsed.value,
        }, handler.handler()));
    }
    try testing.expectEqual(0, handler.calls);
}

test "tools own definitions and give handlers separate argument storage" {
    var handler: Handler = .{ .mode = .mutate };
    var parsed = try std.json.parseFromSlice(std.json.Value, allocator,
        \\{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}
    , .{});
    var tool = sdk.Tool.init(allocator, .{ .name = "echo", .description = "Echo", .parameters = parsed.value }, handler.handler()) catch |err| {
        parsed.deinit();
        return err;
    };
    defer tool.deinit();
    parsed.deinit();
    const arguments = try std.json.parseFromSlice(std.json.Value, allocator, "{\"text\":\"hello\"}", .{});
    defer arguments.deinit();
    var run = std.heap.ArenaAllocator.init(allocator);
    defer run.deinit();
    const output = try tool.execute(run.allocator(), io, arguments.value);
    try testing.expectEqualStrings("Hello", output);
    try testing.expectEqualStrings("hello", arguments.value.object.get("text").?.string);
    try testing.expectEqualStrings("object", tool.definition().parameters.object.get("type").?.string);
}

test "format and defaults remain annotations and extra properties survive" {
    var handler: Handler = .{};
    const parsed = try std.json.parseFromSlice(std.json.Value, allocator,
        \\{"type":"object","properties":{"email":{"type":"string","format":"email"},"count":{"default":7}}}
    , .{});
    defer parsed.deinit();
    var tool = try sdk.Tool.init(allocator, .{ .name = "check", .description = "Check", .parameters = parsed.value }, handler.handler());
    defer tool.deinit();
    const arguments = try std.json.parseFromSlice(std.json.Value, allocator, "{\"email\":\"not an email\",\"extra\":true}", .{});
    defer arguments.deinit();
    var run = std.heap.ArenaAllocator.init(allocator);
    defer run.deinit();
    _ = try tool.execute(run.allocator(), io, arguments.value);
    try testing.expect(arguments.value.object.get("extra").?.bool);
    try testing.expect(!arguments.value.object.contains("count"));
}

test "handler errors and invalid or oversized outputs stay bounded" {
    const schema = try std.json.parseFromSlice(std.json.Value, allocator, "{}", .{});
    defer schema.deinit();
    for ([_]@FieldType(Handler, "mode"){ .fail, .oversized, .invalid }) |mode| {
        var handler: Handler = .{ .mode = mode };
        var tool = try sdk.Tool.init(allocator, .{ .name = "check", .description = "Check", .parameters = schema.value }, handler.handler());
        defer tool.deinit();
        var run = std.heap.ArenaAllocator.init(allocator);
        defer run.deinit();
        try testing.expectError(switch (mode) {
            .fail => error.ToolUnavailable,
            .oversized => error.ToolOutputTooLarge,
            .invalid => error.InvalidToolOutput,
            else => unreachable,
        }, tool.execute(run.allocator(), io, schema.value));
        try testing.expectEqual(1, handler.calls);
    }
}

const WaitingHandler = struct {
    entered: std.Io.Event = .unset,
    cleaned: bool = false,

    fn execute(context: ?*anyopaque, _: std.mem.Allocator, task_io: std.Io, _: std.json.Value) ![]const u8 {
        const self: *WaitingHandler = @ptrCast(@alignCast(context.?));
        defer self.cleaned = true;
        self.entered.set(task_io);
        var wait: std.Io.Event = .unset;
        try wait.wait(task_io);
        return error.UnexpectedWakeup;
    }
};

test "native cancellation interrupts a handler and joins cleanup" {
    const schema = try std.json.parseFromSlice(std.json.Value, allocator, "{}", .{});
    defer schema.deinit();
    var handler: WaitingHandler = .{};
    var tool = try sdk.Tool.init(allocator, .{ .name = "wait", .description = "Wait", .parameters = schema.value }, .{ .context = &handler, .callback = WaitingHandler.execute });
    defer tool.deinit();
    var run = std.heap.ArenaAllocator.init(allocator);
    defer run.deinit();
    var future = try io.concurrent(sdk.Tool.execute, .{ tool, run.allocator(), io, schema.value });
    defer _ = future.cancel(io) catch {};
    try handler.entered.wait(io);
    try testing.expectError(error.Canceled, future.cancel(io));
    try testing.expect(handler.cleaned);
}
