const std = @import("std");
const Io = std.Io;
const Allocator = std.mem.Allocator;
const jsonl = @import("jsonl.zig");
const delivery = @import("delivery.zig");

pub const max_line_bytes = jsonl.max_line_bytes;
const max_queue_bytes = 4_194_304;
const queue_error = "{\"type\":\"protocol_error\",\"error\":{\"code\":\"output_queue_full\",\"message\":\"RPC output queue is full\"}}\n";
const size_error = "{\"type\":\"protocol_error\",\"error\":{\"code\":\"output_too_large\",\"message\":\"RPC output exceeds 1048576 UTF-8 bytes\"}}\n";

// One worker owns native output. Commands enqueue without waiting for I/O;
// prompt acknowledgements and events retain a receipt until delivery finishes.
pub const Output = struct {
    allocator: Allocator,
    io: Io,
    mutex: Io.Mutex = .init,
    ready: Io.Condition = .init,
    failed: Io.Event = .unset,
    done: Io.Event = .unset,
    head: ?*Frame = null,
    tail: ?*Frame = null,
    bytes: usize = 0,
    closing: bool = false,
    failure: ?anyerror = null,

    pub fn enqueue(self: *Output, value: anytype) !*Frame {
        var transferred = false;
        const frame = try self.allocator.create(Frame);
        errdefer if (!transferred) self.allocator.destroy(frame);
        var oversized = false;
        const line = jsonl.encode(self.allocator, value, .{ .emit_null_optional_fields = false }) catch |err| switch (err) {
            error.OutputTooLarge => blk: {
                oversized = true;
                break :blk try self.allocator.dupe(u8, size_error);
            },
            else => return err,
        };
        errdefer if (!transferred) self.allocator.free(line);
        frame.* = .{ .allocator = self.allocator, .line = line, .oversized = oversized, .references = .init(2) };

        self.mutex.lockUncancelable(self.io);
        defer self.mutex.unlock(self.io);
        if (self.closing) return self.failure orelse error.OutputClosed;
        // Keep the complete in-flight line charged until its write finishes,
        // and reserve space for the single queue-overflow diagnostic.
        if (self.bytes + line.len > max_queue_bytes - queue_error.len) {
            const diagnostic = try self.allocator.dupe(u8, queue_error);
            self.allocator.free(line);
            frame.line = diagnostic;
            frame.references.store(1, .monotonic);
            self.closing = true;
            self.failure = error.OutputQueueFull;
            transferred = true;
            self.append(frame);
            self.failed.set(self.io);
            return error.OutputQueueFull;
        }
        transferred = true;
        self.append(frame);
        return frame;
    }

    fn append(self: *Output, frame: *Frame) void {
        if (self.tail) |tail| tail.next = frame else self.head = frame;
        self.tail = frame;
        self.bytes += frame.line.len;
        self.ready.signal(self.io);
    }

    pub fn fail(self: *Output, err: anyerror) void {
        self.mutex.lockUncancelable(self.io);
        defer self.mutex.unlock(self.io);
        if (self.failure == null) self.failure = err;
        self.closing = true;
        self.failed.set(self.io);
        self.ready.broadcast(self.io);
    }

    pub fn wait(self: *Output, frame: *Frame, draining: bool) !void {
        defer frame.release();
        const cancelled = delivery.wait(&frame.done, self.io, draining) catch |err| {
            self.fail(err);
            return err;
        };
        // A delivered final event must not hide cancellation from the core.
        if (cancelled) self.io.recancel();
        if (frame.failure) |err| return err;
        if (frame.oversized) return error.OutputTooLarge;
    }

    pub fn work(self: *Output, file: Io.File) void {
        defer {
            self.mutex.lockUncancelable(self.io);
            defer self.mutex.unlock(self.io);
            while (self.head) |frame| {
                self.head = frame.next;
                frame.finish(self.io, self.failure orelse error.OutputClosed);
            }
            self.tail = null;
            self.bytes = 0;
            self.done.set(self.io);
        }
        while (true) {
            const frame = self.next() catch |err| {
                self.fail(err);
                return;
            } orelse return;
            const failure: ?anyerror = if (file.writeStreamingAll(self.io, frame.line)) |_| null else |err| err;
            self.mutex.lockUncancelable(self.io);
            self.bytes -= frame.line.len;
            self.mutex.unlock(self.io);
            frame.finish(self.io, failure);
            if (failure) |err| {
                self.fail(err);
                return;
            }
        }
    }

    fn next(self: *Output) !?*Frame {
        try self.mutex.lock(self.io);
        defer self.mutex.unlock(self.io);
        while (self.head == null) {
            if (self.closing) return null;
            try self.ready.wait(self.io, &self.mutex);
        }
        const frame = self.head.?;
        self.head = frame.next;
        if (self.head == null) self.tail = null;
        return frame;
    }

    pub fn close(self: *Output, worker: *Io.Future(void)) !void {
        self.mutex.lockUncancelable(self.io);
        self.closing = true;
        self.ready.broadcast(self.io);
        self.mutex.unlock(self.io);
        delivery.drain(&self.done, self.io) catch |err| self.fail(err);
        worker.cancel(self.io);
        if (self.failure) |err| return err;
    }
};

pub const Frame = struct {
    allocator: Allocator,
    line: []u8,
    oversized: bool,
    references: std.atomic.Value(usize),
    next: ?*Frame = null,
    done: Io.Event = .unset,
    failure: ?anyerror = null,

    pub fn release(self: *Frame) void {
        if (self.references.fetchSub(1, .acq_rel) == 1) {
            self.allocator.free(self.line);
            self.allocator.destroy(self);
        }
    }

    fn finish(self: *Frame, io: Io, failure: ?anyerror) void {
        self.failure = failure;
        self.done.set(io);
        self.release();
    }
};
