const std = @import("std");
const sdk = @import("tinyagent");

fn echo(_: ?*anyopaque, _: std.mem.Allocator, io: std.Io, request: sdk.ProviderRequest, text: sdk.TextHandler) !sdk.ProviderTurn {
    const message = request.history[request.history.len - 1].user;
    try text.emit(io, message);
    return .{ .assistant = .{ .text = message } };
}

pub fn main(init: std.process.Init) !void {
    const agent: sdk.Agent = .{ .provider = .{ .stream = echo }, .model = "offline" };
    const server = try sdk.HttpServer.start(init.gpa, init.io, agent, .{ .port = 0 });
    defer server.deinit();
    var output = std.Io.File.stdout().writerStreaming(init.io, &.{});
    try output.interface.print("{s}\n", .{server.url});
    // Keep serving until the embedding application requests server.stop().
    // This example uses stdin EOF as its shutdown request.
    var input = std.Io.File.stdin().readerStreaming(init.io, &.{});
    _ = try input.interface.discardRemaining();
}
