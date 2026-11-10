const std = @import("std");
const sdk = @import("tinyagent");
const testing = std.testing;
const allocator = testing.allocator;
const io = testing.io;

const Provider = struct {
    lengths: std.ArrayList(usize) = .empty,

    fn stream(context: ?*anyopaque, memory: std.mem.Allocator, task_io: std.Io, request: sdk.ProviderRequest, text: sdk.TextHandler) !sdk.ProviderTurn {
        const self: *Provider = @ptrCast(@alignCast(context.?));
        try self.lengths.append(allocator, request.history.len);
        const last = request.history[request.history.len - 1];
        if (last == .tool_result) return .{ .assistant = .{ .text = "tool result" } };
        const prompt = last.user;
        if (std.mem.eql(u8, prompt, "fail")) {
            try text.emit(task_io, "partial");
            return error.ProviderUnavailable;
        }
        if (std.mem.eql(u8, prompt, "tool")) {
            const arguments = try std.json.parseFromSlice(std.json.Value, memory, "{\"command\":\"printf observation\"}", .{});
            const calls = try memory.alloc(sdk.ToolCall, 1);
            calls[0] = .{ .call_id = "bash-1", .name = "bash", .arguments = arguments.value };
            try text.emit(task_io, "before");
            return .{ .assistant = .{ .text = "before", .tool_calls = calls } };
        }
        try text.emit(task_io, prompt);
        return .{ .assistant = .{ .text = prompt } };
    }
};

test "console streams once, runs Bash through the core, and rolls back failed prompts" {
    var directory = testing.tmpDir(.{});
    defer directory.cleanup();
    try directory.dir.writeFile(io, .{ .sub_path = "input", .data = "first\nfail\nnext\ntool\n" });
    const input = try directory.dir.openFile(io, "input", .{});
    defer input.close(io);
    const output = try directory.dir.createFile(io, "output", .{});
    defer output.close(io);
    const diagnostics = try directory.dir.createFile(io, "diagnostics", .{});
    defer diagnostics.close(io);
    var provider: Provider = .{};
    defer provider.lengths.deinit(allocator);
    var tool = try sdk.bashTool(allocator);
    defer tool.deinit();
    const agent: sdk.Agent = .{ .provider = .{ .context = &provider, .stream = Provider.stream }, .model = "offline", .tools = &.{&tool} };
    try sdk.console.run(allocator, io, agent, .{ .input = input, .output = output, .diagnostics = diagnostics }, null);
    try testing.expectEqualSlices(usize, &.{ 1, 3, 3, 5, 7 }, provider.lengths.items);
    var buffer: [1024]u8 = undefined;
    try testing.expectEqualStrings(
        "TinyAgent. Type a task, or Ctrl-C or Ctrl-D to exit.\n\n> first\n\n> partial\n> next\n\n> beforetool result\n\n> ",
        try directory.dir.readFile(io, "output", &buffer),
    );
    try testing.expectEqualStrings(
        "error: ProviderUnavailable\n\n$ printf observation\nexit_code: 0\nstdout:\nobservationstderr:\n\n",
        try directory.dir.readFile(io, "diagnostics", &buffer),
    );
    // The adapter borrows the files, including their final positions.
    try output.writeStreamingAll(io, "owned");
    try diagnostics.writeStreamingAll(io, "owned");
    try testing.expectError(error.EndOfStream, input.readStreaming(io, &.{&buffer}));
}

test "CLI modes and serve options are validated before constructing a provider" {
    try testing.expectEqual(.console, try sdk.cli.parse(&.{}, null));
    try testing.expectEqual(.self_test, try sdk.cli.parse(&.{"--self-test"}, null));
    try testing.expectEqual(.rpc, try sdk.cli.parse(&.{ "--mode", "rpc" }, null));
    const mode = try sdk.cli.parse(&.{ "serve", "--host", "::1", "--port", "0", "--token", "secret", "--allow-remote" }, "default");
    try testing.expectEqualStrings("::1", mode.http.host);
    try testing.expectEqual(0, mode.http.port);
    try testing.expectEqualStrings("secret", mode.http.token.?);
    try testing.expect(mode.http.allow_remote);
    const defaults = try sdk.cli.parse(&.{ "serve", "--host", "localhost" }, "default");
    try testing.expectEqualStrings("127.0.0.1", defaults.http.host);
    try testing.expectEqualStrings("default", defaults.http.token.?);
    for ([_][]const []const u8{ &.{"--mode"}, &.{ "--mode", "bad" }, &.{"unknown"}, &.{ "serve", "--port" }, &.{ "serve", "--bad", "value" } }) |args| {
        try testing.expectError(error.InvalidArguments, sdk.cli.parse(args, null));
    }
    for ([_][]const u8{ "", "-1", "+1", "65536", "1.5" }) |port| try testing.expectError(error.InvalidPort, sdk.cli.parse(&.{ "serve", "--port", port }, null));
    try testing.expectError(error.InvalidHost, sdk.cli.parse(&.{ "serve", "--host", "example.com" }, null));
}
