const std = @import("std");
const Session = @import("session.zig").Session;
const Event = @import("types.zig").Event;
const jsonl = @import("jsonl.zig");
const delivery = @import("delivery.zig");
const Io = std.Io;

/// One prompt response. The session owns history and the agent loop; this
/// value owns HTTP delivery and the concurrent disconnect observation.
pub const Prompt = struct {
    allocator: std.mem.Allocator,
    io: Io,
    stream: Io.net.Stream,
    session: *Session,
    started: bool = false,
    mutex: Io.Mutex = .init,
    terminal: bool = false,
    disconnected: bool = false,

    pub fn run(self: *Prompt, message: []const u8) !void {
        const Outcome = union(enum) { run: anyerror!void, disconnected: Io.Cancelable!bool };
        var outcomes: [2]Outcome = undefined;
        var select: Io.Select(Outcome) = .init(self.io, &outcomes);
        defer select.cancelDiscard();
        try select.concurrent(.run, Prompt.execute, .{ self, message });
        try select.concurrent(.disconnected, Prompt.watch, .{self});
        var result = try select.await();
        if (result == .disconnected) {
            if (try result.disconnected) return error.Canceled;
            // EOF during final delivery shuts down the socket to release a
            // blocked write, then lets that write's result decide the commit.
            result = try select.await();
        }
        std.debug.assert(result == .run);
        select.cancelDiscard();
        try result.run;
    }

    fn execute(self: *Prompt, message: []const u8) !void {
        var result = try self.session.run(message, .{
            .before_run = .{ .context = self, .callback = headers },
            .on_event = .{ .context = self, .callback = event },
        });
        result.deinit();
    }

    fn headers(context: ?*anyopaque, _: Io, _: []const u8) !void {
        const self: *Prompt = @ptrCast(@alignCast(context.?));
        self.started = true;
        try self.send("HTTP/1.1 200 OK\r\nContent-Type: application/jsonl; charset=utf-8\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n", false, false);
    }

    fn event(context: ?*anyopaque, _: Io, value: Event) !void {
        const self: *Prompt = @ptrCast(@alignCast(context.?));
        var oversized = false;
        const line = jsonl.encode(self.allocator, value, .{}) catch |err| switch (err) {
            error.OutputTooLarge => blk: {
                oversized = true;
                break :blk try jsonl.encode(self.allocator, .{
                    .type = "agent_end",
                    .runId = value.run_id,
                    .sequence = value.sequence,
                    .status = "failed",
                    .@"error" = .{ .code = "output_too_large", .message = "HTTP output exceeds 1048576 UTF-8 bytes", .retryable = false },
                }, .{});
            },
            else => return err,
        };
        defer self.allocator.free(line);
        const terminal = oversized or switch (value.kind) {
            .completed, .failed, .aborted => true,
            else => false,
        };
        if (terminal) {
            self.mutex.lockUncancelable(self.io);
            const disconnected = self.disconnected;
            self.terminal = true;
            self.mutex.unlock(self.io);
            if (disconnected) return error.Canceled;
        }
        if (oversized) _ = self.session.cancel();
        try self.send(line, terminal, oversized or value.kind == .aborted);
        if (oversized) return error.OutputTooLarge;
    }

    fn send(self: *Prompt, bytes: []const u8, terminal: bool, draining: bool) !void {
        var transfer: Transfer = .{ .io = self.io, .stream = self.stream, .bytes = bytes, .terminal = terminal };
        var worker = try self.io.concurrent(Transfer.write, .{&transfer});
        defer worker.cancel(self.io);
        const cancelled = try delivery.wait(&transfer.done, self.io, draining);
        if (cancelled) self.io.recancel();
        try transfer.result.?;
    }

    fn watch(self: *Prompt) Io.Cancelable!bool {
        var buffer: [256]u8 = undefined;
        var reader = self.stream.reader(self.io, &buffer);
        if (reader.interface.discardRemaining()) |_| {} else |_| {
            if (reader.err) |err| if (err == error.Canceled) return error.Canceled;
        }
        self.mutex.lockUncancelable(self.io);
        self.disconnected = true;
        const cancel = !self.terminal;
        self.mutex.unlock(self.io);
        self.stream.shutdown(self.io, .both) catch {};
        return cancel;
    }
};

const Transfer = struct {
    io: Io,
    stream: Io.net.Stream,
    bytes: []const u8,
    terminal: bool,
    done: Io.Event = .unset,
    result: ?anyerror!void = null,

    fn write(self: *Transfer) void {
        defer self.done.set(self.io);
        self.result = self.perform();
    }

    fn perform(self: *Transfer) !void {
        var writer = self.stream.writer(self.io, &.{});
        writer.interface.writeAll(self.bytes) catch |err| return writer.err orelse err;
        // Connection-close framing belongs to terminal delivery. The session
        // cannot commit merely because the final JSON line left its callback.
        if (self.terminal) try self.stream.shutdown(self.io, .send);
    }
};
