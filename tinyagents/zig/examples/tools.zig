const std = @import("std");
const tinyagent = @import("tinyagent");

fn echo(_: ?*anyopaque, _: std.mem.Allocator, _: std.Io, arguments: std.json.Value) ![]const u8 {
    return arguments.object.get("text").?.string;
}

fn provider(_: ?*anyopaque, memory: std.mem.Allocator, io: std.Io, request: tinyagent.ProviderRequest, on_text: tinyagent.TextHandler) !tinyagent.ProviderTurn {
    const latest = request.history[request.history.len - 1];
    if (latest == .tool_result) {
        try on_text.emit(io, latest.tool_result.output);
        return .{ .assistant = .{ .text = latest.tool_result.output } };
    }
    var arguments: std.json.ObjectMap = .empty;
    try arguments.put(memory, "text", .{ .string = latest.user });
    const calls = try memory.alloc(tinyagent.ToolCall, 1);
    calls[0] = .{ .call_id = "c_echo", .name = "echo", .arguments = .{ .object = arguments } };
    return .{ .assistant = .{ .text = "", .tool_calls = calls } };
}

pub fn main(init: std.process.Init) !void {
    const schema = try std.json.parseFromSlice(std.json.Value, init.gpa,
        \\{"type":"object","properties":{"text":{"type":"string"}},"required":["text"],"additionalProperties":false}
    , .{});
    defer schema.deinit();
    var tool = try tinyagent.Tool.init(init.gpa, .{
        .name = "echo",
        .description = "Echo text",
        .parameters = schema.value,
    }, .{ .callback = echo });
    defer tool.deinit();
    const agent: tinyagent.Agent = .{
        .model = "offline",
        .provider = .{ .stream = provider },
        .tools = &.{&tool},
    };
    var result = try agent.run(init.gpa, init.io, "Hello", .{});
    defer result.deinit();
    var output = std.Io.File.stdout().writer(init.io, &.{});
    try output.interface.print("{s}\n", .{result.text});
}
