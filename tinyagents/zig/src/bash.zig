const std = @import("std");
const builtin = @import("builtin");
const Tool = @import("tool.zig").Tool;
const Allocator = std.mem.Allocator;
const Io = std.Io;
const Child = std.process.Child;
const output_limit = 131_072;

const denied_words = [_][]const u8{
    "rm",   "rmdir", "unlink",   "shred",  "truncate", "dd",   "mkfs", "wipefs", "fdisk",   "parted",
    "sudo", "doas",  "shutdown", "reboot", "poweroff", "halt", "kill", "pkill",  "killall",
};

/// The coding CLI's blacklist is a guardrail, not a shell sandbox. The returned
/// reason is static text; only normalization uses the supplied allocator.
pub fn deniedReason(allocator: Allocator, command: []const u8) !?[]const u8 {
    const normalized = try allocator.alloc(u8, command.len);
    defer allocator.free(normalized);
    var length: usize = 0;
    for (command) |byte| {
        if (byte == '\\' or byte == '"' or byte == '\'') continue;
        normalized[length] = std.ascii.toLower(byte);
        length += 1;
    }
    const clean = normalized[0..length];
    var has_find = false;
    var has_delete = false;
    var has_git = false;
    var has_clean = false;
    var has_restore = false;
    var has_reset = false;
    var has_hard = false;
    var has_checkout = false;
    var has_double_dash = false;
    var words = std.mem.tokenizeAny(u8, clean, " \t\r\n/;:&|(){}[],$=`");
    while (words.next()) |word| {
        for (denied_words) |denied| {
            if (std.mem.eql(u8, word, denied)) return "destructive command is denied";
        }
        if (std.mem.startsWith(u8, word, "mkfs.")) return "destructive command is denied";
        has_find = has_find or std.mem.eql(u8, word, "find");
        has_delete = has_delete or std.mem.eql(u8, word, "-delete");
        has_git = has_git or std.mem.eql(u8, word, "git");
        has_clean = has_clean or std.mem.eql(u8, word, "clean");
        has_restore = has_restore or std.mem.eql(u8, word, "restore");
        has_reset = has_reset or std.mem.eql(u8, word, "reset");
        has_hard = has_hard or std.mem.eql(u8, word, "--hard");
        has_checkout = has_checkout or std.mem.eql(u8, word, "checkout");
        has_double_dash = has_double_dash or std.mem.eql(u8, word, "--");
    }
    if (has_find and has_delete) return "'find -delete' is denied";
    if (has_git and (has_clean or has_restore or (has_reset and has_hard) or (has_checkout and has_double_dash)))
        return "destructive git command is denied";
    length = 0;
    for (clean) |byte| {
        if (std.ascii.isWhitespace(byte)) continue;
        normalized[length] = byte;
        length += 1;
    }
    if (std.mem.indexOf(u8, normalized[0..length], ":(){:|:&};:") != null) return "fork bomb is denied";
    return null;
}

pub fn bashTool(allocator: Allocator) !Tool {
    const schema = try std.json.parseFromSlice(std.json.Value, allocator,
        \\{"type":"object","properties":{"command":{"type":"string","minLength":1}},"required":["command"],"additionalProperties":false}
    , .{});
    defer schema.deinit();
    return Tool.init(allocator, .{
        .name = "bash",
        .description = "Run a non-interactive bash command in the current directory.",
        .parameters = schema.value,
    }, .{ .callback = execute });
}

fn execute(_: ?*anyopaque, allocator: Allocator, io: Io, arguments: std.json.Value) ![]const u8 {
    return runBash(allocator, io, arguments.object.get("command").?.string);
}

/// Runs in the current directory with closed stdin. Caller owns the returned
/// observation. Native cancellation and output overflow terminate the process
/// group on POSIX, or the direct child on Windows, and join cleanup.
pub fn runBash(allocator: Allocator, io: Io, command: []const u8) ![]const u8 {
    try io.checkCancel();
    if (try deniedReason(allocator, command)) |reason| {
        return std.fmt.allocPrint(allocator, "exit_code: 126\nstdout:\n\nstderr:\nblocked: {s}\n", .{reason});
    }
    var child = try std.process.spawn(io, .{
        .argv = &.{ "bash", "-c", command },
        .stdin = .ignore,
        .stdout = .pipe,
        .stderr = .pipe,
        .pgid = if (builtin.os.tag == .windows) null else 0,
    });
    defer if (child.id) |id| {
        terminate(id);
        child.kill(io);
    };

    var buffer: Io.File.MultiReader.Buffer(2) = undefined;
    var pipes: Io.File.MultiReader = undefined;
    pipes.init(allocator, io, buffer.toStreams(), &.{ child.stdout.?, child.stderr.? });
    defer pipes.deinit();
    const stdout = pipes.reader(0);
    const stderr = pipes.reader(1);
    while (pipes.fill(4096, .none)) |_| {
        if (stdout.bufferedLen() + stderr.bufferedLen() > output_limit) return error.ToolOutputTooLarge;
        try pipes.checkAnyError();
    } else |err| switch (err) {
        error.EndOfStream => {},
        else => return err,
    }
    try pipes.checkAnyError();

    // Do not cancel child.wait itself: Zig 0.16 clears its process handle on
    // cancellation. The owner terminates the group, then joins an uncancelable
    // native wait. Retain ownership until EOF even if the shell exited earlier.
    const term = blk: {
        const Outcome = union(enum) { term: Child.WaitError!Child.Term };
        var outcomes: [1]Outcome = undefined;
        var select: Io.Select(Outcome) = .init(io, &outcomes);
        defer select.cancelDiscard();
        const id = child.id.?;
        try select.concurrent(.term, wait, .{ &child, io });
        const outcome = select.await() catch |err| {
            terminate(id);
            return err;
        };
        break :blk try outcome.term;
    };

    const exit_code: i32 = switch (term) {
        .exited => |code| code,
        else => -1,
    };
    const output = try std.fmt.allocPrint(allocator, "exit_code: {d}\nstdout:\n{s}stderr:\n{s}", .{ exit_code, stdout.buffered(), stderr.buffered() });
    errdefer allocator.free(output);
    if (output.len > output_limit) return error.ToolOutputTooLarge;
    if (!std.unicode.utf8ValidateSlice(output)) return error.InvalidToolOutput;
    return output;
}

fn wait(child: *Child, io: Io) Child.WaitError!Child.Term {
    const protection = io.swapCancelProtection(.blocked);
    defer _ = io.swapCancelProtection(protection);
    return child.wait(io);
}

fn terminate(id: Child.Id) void {
    if (builtin.os.tag == .windows) {
        _ = std.os.windows.ntdll.NtTerminateProcess(id, @enumFromInt(1));
    } else {
        std.posix.kill(-id, .KILL) catch {};
    }
}
