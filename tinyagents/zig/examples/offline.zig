const std = @import("std");
const tinyagent = @import("tinyagent");

fn echo(_: ?*anyopaque, allocator: std.mem.Allocator, io: std.Io, request: tinyagent.ProviderRequest, on_text: tinyagent.TextHandler) !tinyagent.ProviderTurn {
    const prompt = request.history[request.history.len - 1].user;
    const text = try std.fmt.allocPrint(allocator, "You said: {s}", .{prompt});
    try on_text.emit(io, text);
    return .{ .assistant = .{ .text = text } };
}

pub fn main(init: std.process.Init) !void {
    const agent: tinyagent.Agent = .{ .provider = .{ .stream = echo }, .model = "offline" };
    var result = try agent.run(init.gpa, init.io, "Hello", .{});
    defer result.deinit();
    var output = std.Io.File.stdout().writer(init.io, &.{});
    try output.interface.print("{s}\n", .{result.text});
}
