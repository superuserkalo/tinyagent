const std = @import("std");
const sdk = @import("tinyagent");
const Io = std.Io;

var original_io: Io = undefined;
var release: Io.Event = .unset;
var pause_final: std.atomic.Value(bool) = .init(false);

// Keep native socket operations intact. Restrict the send buffer so the
// subprocess tests can exercise actual backpressure with a small TCP window.
fn write(context: ?*anyopaque, socket: Io.net.Socket.Handle, header: []const u8, data: []const []const u8, splat: usize) Io.net.Stream.Writer.Error!usize {
    const size: c_int = 4096;
    std.posix.setsockopt(socket, std.posix.SOL.SOCKET, std.posix.SO.SNDBUF, std.mem.asBytes(&size)) catch return error.Unexpected;
    return original_io.vtable.netWrite(context, socket, header, data, splat);
}

// Force client EOF to reach the watcher after native final framing succeeds
// but before the writer reports that success. Production has no test hooks.
fn shutdown(context: ?*anyopaque, socket: Io.net.Socket.Handle, how: Io.net.ShutdownHow) Io.net.ShutdownError!void {
    const result = original_io.vtable.netShutdown(context, socket, how);
    if (how == .both) Io.File.stderr().writeStreamingAll(original_io, "disconnected\n") catch {};
    try result;
    if (how == .send and pause_final.swap(false, .acq_rel)) {
        Io.File.stderr().writeStreamingAll(original_io, "framing delivered\n") catch return error.Unexpected;
        release.waitUncancelable(original_io);
    }
}

const Provider = struct {
    server: ?*sdk.HttpServer = null,

    fn stream(context: ?*anyopaque, allocator: std.mem.Allocator, io: Io, request: sdk.ProviderRequest, text: sdk.TextHandler) !sdk.ProviderTurn {
        const self: *Provider = @ptrCast(@alignCast(context.?));
        const prompt = request.history[request.history.len - 1].user;
        defer {
            const protection = io.swapCancelProtection(.blocked);
            defer _ = io.swapCancelProtection(protection);
            Io.File.stderr().writeStreamingAll(io, "cleanup\n") catch {};
            if (std.mem.eql(u8, prompt, "hold")) release.waitUncancelable(io);
        }
        if (std.mem.eql(u8, prompt, "stop")) {
            self.server.?.stop();
            try Io.File.stderr().writeStreamingAll(io, "callback stopped\n");
            try io.checkCancel();
        }
        if (std.mem.eql(u8, prompt, "wait") or std.mem.eql(u8, prompt, "hold") or std.mem.eql(u8, prompt, "silent")) {
            if (!std.mem.eql(u8, prompt, "silent")) try text.emit(io, "waiting");
            try Io.File.stderr().writeStreamingAll(io, "waiting\n");
            var forever: Io.Event = .unset;
            try forever.wait(io);
        }
        if (std.mem.eql(u8, prompt, "fail")) {
            try text.emit(io, "partial");
            return error.ProviderUnavailable;
        }
        if (std.mem.eql(u8, prompt, "history")) {
            const value = try std.fmt.allocPrint(allocator, "{d}", .{request.history.len});
            return .{ .assistant = .{ .text = value } };
        }
        if (std.mem.eql(u8, prompt, "oversize") or std.mem.eql(u8, prompt, "oversize_final") or
            std.mem.eql(u8, prompt, "large") or std.mem.eql(u8, prompt, "final_large"))
        {
            const oversized = std.mem.startsWith(u8, prompt, "oversize");
            const value = try allocator.alloc(u8, if (oversized) 1_048_576 else 900_000);
            @memset(value, 'x');
            if (std.mem.eql(u8, prompt, "large")) {
                for (0..30) |_| try text.emit(io, value);
            } else if (std.mem.eql(u8, prompt, "oversize")) try text.emit(io, value);
            return .{ .assistant = .{ .text = value } };
        }
        if (std.mem.eql(u8, prompt, "pause_final")) pause_final.store(true, .release);
        try text.emit(io, prompt);
        return .{ .assistant = .{ .text = prompt } };
    }
};

pub fn main(init: std.process.Init) !void {
    original_io = init.io;
    var vtable = init.io.vtable.*;
    vtable.netWrite = write;
    vtable.netShutdown = shutdown;
    const io: Io = .{ .userdata = init.io.userdata, .vtable = &vtable };
    const args = try init.minimal.args.toSlice(init.arena.allocator());
    var provider: Provider = .{};
    const agent: sdk.Agent = .{ .provider = .{ .context = &provider, .stream = Provider.stream }, .model = "offline" };
    const server = sdk.HttpServer.start(init.gpa, io, agent, .{
        .host = if (args.len > 1) args[1] else "127.0.0.1",
        .port = 0,
        .token = if (args.len > 2 and !std.mem.eql(u8, args[2], "-")) args[2] else null,
        .allow_remote = args.len > 3 and std.mem.eql(u8, args[3], "true"),
    }) catch |err| {
        var output = Io.File.stdout().writerStreaming(io, &.{});
        try output.interface.print("error:{s}\n", .{@errorName(err)});
        return;
    };
    provider.server = server;
    defer server.deinit();
    var output = Io.File.stdout().writerStreaming(io, &.{});
    try output.interface.print("{s}\n", .{server.url});
    var buffer: [8192]u8 = undefined;
    var input = Io.File.stdin().readerStreaming(io, &buffer);
    while (try input.interface.takeDelimiter('\n')) |line| {
        if (std.mem.eql(u8, line, "release")) {
            release.set(io);
        } else if (std.mem.eql(u8, line, "stop")) {
            server.stop();
        } else if (std.mem.startsWith(u8, line, "references ")) {
            // Observe request admission under the store's lock. The test can
            // then delete while a cancel request is waiting for its body.
            server.sessions.mutex.lockUncancelable(io);
            const references = if (server.sessions.entries.get(line[11..])) |entry| entry.references else 0;
            server.sessions.mutex.unlock(io);
            try output.interface.print("{d}\n", .{references});
        } else if (std.mem.startsWith(u8, line, "state ")) {
            const entry = server.sessions.acquire(line[6..]);
            if (entry) |value| {
                defer server.sessions.release(value);
                const state = value.session.state();
                try output.interface.print("{{\"running\":{s},\"messages\":{d}}}\n", .{
                    if (state.run_id != null) "true" else "false", state.message_count,
                });
            } else try output.interface.writeAll("null\n");
        }
    }
}
