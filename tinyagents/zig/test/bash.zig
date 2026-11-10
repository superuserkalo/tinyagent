const std = @import("std");
const builtin = @import("builtin");
const sdk = @import("tinyagent");
const testing = std.testing;
const allocator = testing.allocator;
const io = testing.io;

test "Bash retains the CLI blacklist and captures both streams and exit status" {
    try testing.expectEqual(null, try sdk.deniedReason(allocator, "printf 'hello\\n'"));
    for ([_][]const u8{
        "rm -rf /",             "r\"\"m -rf /",       "sudo echo hi",  "git reset --hard HEAD",
        "find . -delete",       "mkfs.ext4 /dev/sda", "git clean -fd", "git restore .",
        "git checkout -- file", ":() { : | : & }; :",
    }) |command| try testing.expect(try sdk.deniedReason(allocator, command) != null);
    const output = try sdk.runBash(allocator, io, "printf 'hello\\n'; printf 'warning\\n' >&2; exit 7");
    defer allocator.free(output);
    try testing.expectEqualStrings("exit_code: 7\nstdout:\nhello\nstderr:\nwarning\n", output);
    const blocked = try sdk.runBash(allocator, io, "sudo echo hi");
    defer allocator.free(blocked);
    try testing.expect(std.mem.startsWith(u8, blocked, "exit_code: 126\n"));
    const no_input = try sdk.runBash(allocator, io, "cat");
    defer allocator.free(no_input);
    try testing.expectEqualStrings("exit_code: 0\nstdout:\nstderr:\n", no_input);
}

test "Bash is an ordinary checked SDK tool" {
    var tool = try sdk.bashTool(allocator);
    defer tool.deinit();
    try testing.expectEqualStrings("bash", tool.definition().name);
    var arena = std.heap.ArenaAllocator.init(allocator);
    defer arena.deinit();
    for ([_][]const u8{ "{}", "{\"command\":3}", "{\"command\":\"\"}", "{\"command\":\"printf ok\",\"extra\":true}" }) |raw| {
        const arguments = try std.json.parseFromSlice(std.json.Value, allocator, raw, .{});
        defer arguments.deinit();
        try testing.expectError(error.InvalidToolArguments, tool.execute(arena.allocator(), io, arguments.value));
    }
    const arguments = try std.json.parseFromSlice(std.json.Value, allocator, "{\"command\":\"printf café\"}", .{});
    defer arguments.deinit();
    try testing.expectEqualStrings("exit_code: 0\nstdout:\ncaféstderr:\n", try tool.execute(arena.allocator(), io, arguments.value));
}

test "Bash bounds combined output and includes observation framing in the limit" {
    for ([_][]const u8{
        "while :; do printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'; done",
        "while :; do printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx' >&2; done",
        "printf '%70000s' ''; printf '%70000s' '' >&2",
    }) |command| try testing.expectError(error.ToolOutputTooLarge, sdk.runBash(allocator, io, command));
    const framing = "exit_code: 0\nstdout:\nstderr:\n".len;
    for ([_]usize{ 131_072 - framing, 131_073 - framing }) |length| {
        const command = try std.fmt.allocPrint(allocator, "printf '%{d}s' ''", .{length});
        defer allocator.free(command);
        if (length + framing == 131_072) {
            const output = try sdk.runBash(allocator, io, command);
            defer allocator.free(output);
            try testing.expectEqual(131_072, output.len);
        } else {
            try testing.expectError(error.ToolOutputTooLarge, sdk.runBash(allocator, io, command));
        }
    }
    try testing.expectError(error.InvalidToolOutput, sdk.runBash(allocator, io, "printf '\\377'"));
}

test "cancelling Bash stops descendants with open or closed pipes and reaps the shell" {
    if (builtin.os.tag == .windows) return error.SkipZigTest;
    for ([_][]const u8{ "; wait", "", "; exec 1>&- 2>&-; wait" }) |ending| {
        var directory = testing.tmpDir(.{});
        defer directory.cleanup();
        const command = try std.fmt.allocPrint(
            allocator,
            "sleep 30 & printf '%s %s\\n' $$ $! > '.zig-cache/tmp/{s}/ready'{s}",
            .{ directory.sub_path, ending },
        );
        defer allocator.free(command);
        var running = try io.concurrent(sdk.runBash, .{ allocator, io, command });
        defer if (running.cancel(io)) |output| allocator.free(output) else |_| {};
        const deadline = std.Io.Clock.Timestamp.fromNow(io, .{ .raw = .fromSeconds(3), .clock = .awake });
        var bytes: [64]u8 = undefined;
        const ready = while (true) {
            if (directory.dir.readFile(io, "ready", &bytes)) |content| {
                if (std.mem.endsWith(u8, content, "\n")) break content;
            } else |err| if (err != error.FileNotFound) return err;
            if (deadline.durationFromNow(io).raw.nanoseconds <= 0) return error.BashDidNotStart;
            try io.sleep(.fromMilliseconds(5), .awake);
        };
        var words = std.mem.tokenizeAny(u8, ready, " \n");
        const shell = try std.fmt.parseInt(std.posix.pid_t, words.next().?, 10);
        const descendant = try std.fmt.parseInt(std.posix.pid_t, words.next().?, 10);
        // Let the closed-pipe case reach the native child wait.
        try io.sleep(.fromMilliseconds(100), .awake);
        const stopped = std.Io.Clock.Timestamp.fromNow(io, .{ .raw = .fromSeconds(2), .clock = .awake });
        try testing.expectError(error.Canceled, running.cancel(io));
        try testing.expect(stopped.durationFromNow(io).raw.nanoseconds > 0);
        try testing.expectError(error.ProcessNotFound, std.posix.kill(shell, @enumFromInt(0)));
        while (std.posix.kill(descendant, @enumFromInt(0))) |_| {
            if (stopped.durationFromNow(io).raw.nanoseconds <= 0) return error.BashDescendantSurvived;
            try io.sleep(.fromMilliseconds(5), .awake);
        } else |err| try testing.expectEqual(error.ProcessNotFound, err);
    }
}
