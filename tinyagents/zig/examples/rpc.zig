const std = @import("std");
const sdk = @import("tinyagent");

fn echo(_: ?*anyopaque, _: std.mem.Allocator, io: std.Io, request: sdk.ProviderRequest, text: sdk.TextHandler) !sdk.ProviderTurn {
    const prompt = request.history[request.history.len - 1].user;
    try text.emit(io, prompt);
    return .{ .assistant = .{ .text = prompt } };
}

pub fn main(init: std.process.Init) !void {
    const agent: sdk.Agent = .{ .provider = .{ .stream = echo }, .model = "offline" };
    try sdk.runRpc(init.gpa, init.io, agent, .stdin(), .stdout(), .{});
}
