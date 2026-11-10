const std = @import("std");
const Agent = @import("agent.zig").Agent;
const Session = @import("session.zig").Session;
const Event = @import("types.zig").Event;
const delivery = @import("delivery.zig");
const Io = std.Io;

pub const Files = struct {
    input: Io.File,
    output: Io.File,
    diagnostics: Io.File,
};

/// Run a plain conversation using the shared session and Agent loop. Files and
/// their modes remain caller-owned. Cancellation joins provider/tool cleanup;
/// a pending output write gets one second to finish before it is cancelled.
pub fn run(allocator: std.mem.Allocator, io: Io, agent: Agent, files: Files, cancellation: ?*Io.Event) !void {
    var session = Session.init(allocator, io, agent);
    defer session.deinit();
    var console: Console = .{ .allocator = allocator, .io = io, .session = &session, .files = files };
    const Outcome = union(enum) { work: void, cancelled: Io.Cancelable!void };
    var outcomes: [2]Outcome = undefined;
    var select: Io.Select(Outcome) = .init(io, &outcomes);
    defer {
        console.stopping.set(io);
        select.cancelDiscard();
    }
    try select.concurrent(.work, Console.work, .{&console});
    if (cancellation) |signal| try select.concurrent(.cancelled, Io.Event.wait, .{ signal, io });
    _ = select.await() catch {};
    console.stopping.set(io);
    select.cancelDiscard();
    if (console.failure) |err| return err;
    console.result.? catch |err| switch (err) {
        error.Canceled => {},
        else => return err,
    };
}

const Console = struct {
    allocator: std.mem.Allocator,
    io: Io,
    session: *Session,
    files: Files,
    stopping: Io.Event = .unset,
    streamed: bool = false,
    failure: ?anyerror = null,
    result: ?anyerror!void = null,

    fn work(self: *Console) void {
        self.result = self.read();
    }

    fn read(self: *Console) !void {
        try self.send(self.files.output, "TinyAgent. Type a task, or Ctrl-C or Ctrl-D to exit.\n", .{});
        try self.send(self.files.output, "\n> ", .{});
        var partial: std.ArrayList(u8) = .empty;
        defer partial.deinit(self.allocator);
        var buffer: [8192]u8 = undefined;
        while (true) {
            const count = self.files.input.readStreaming(self.io, &.{&buffer}) catch |err| switch (err) {
                error.EndOfStream => {
                    if (partial.items.len != 0) try self.prompt(partial.items);
                    return;
                },
                else => return err,
            };
            var remaining = buffer[0..count];
            while (remaining.len != 0) {
                const newline = std.mem.indexOfScalar(u8, remaining, '\n');
                const length = newline orelse remaining.len;
                if (partial.items.len + length > 1_048_576) return error.InputTooLarge;
                try partial.appendSlice(self.allocator, remaining[0..length]);
                remaining = remaining[length + @intFromBool(newline != null) ..];
                if (newline != null) {
                    try self.prompt(partial.items);
                    partial.clearRetainingCapacity();
                    try self.send(self.files.output, "\n> ", .{});
                }
            }
        }
    }

    fn prompt(self: *Console, bytes: []const u8) !void {
        if (!std.unicode.utf8ValidateSlice(bytes)) return error.InvalidUtf8;
        const message = std.mem.trim(u8, bytes, " \t\r\n");
        if (message.len == 0) return;
        self.streamed = false;
        var result = self.session.run(message, .{ .on_event = .{ .context = self, .callback = event } }) catch |err| {
            if (self.failure) |failure| return failure;
            if (err == error.Canceled or self.stopping.isSet()) return error.Canceled;
            try self.send(self.files.diagnostics, "error: {s}\n", .{@errorName(err)});
            return;
        };
        result.deinit();
    }

    fn event(context: ?*anyopaque, _: Io, value: Event) !void {
        const self: *Console = @ptrCast(@alignCast(context.?));
        self.render(value) catch |err| {
            self.failure = err;
            return err;
        };
    }

    fn render(self: *Console, value: Event) !void {
        switch (value.kind) {
            .text_delta => |text| {
                self.streamed = true;
                try self.send(self.files.output, "{s}", .{text});
            },
            .tool_execution_start => |call| {
                if (call.arguments.object.get("command")) |command| {
                    if (command == .string) try self.send(self.files.diagnostics, "\n$ {s}\n", .{command.string});
                }
            },
            .tool_execution_end => |result| {
                self.streamed = false;
                try self.send(self.files.diagnostics, "{s}\n", .{result.output});
            },
            .completed => |text| try self.send(self.files.output, "{s}\n", .{if (self.streamed) "" else text}),
            else => {},
        }
    }

    fn send(self: *Console, file: Io.File, comptime format: []const u8, args: anytype) !void {
        const bytes = try std.fmt.allocPrint(self.allocator, format, args);
        defer self.allocator.free(bytes);
        try write(self.io, file, bytes, self.stopping.isSet());
    }
};

/// Package helper used for CLI startup/errors as well as console events.
pub fn write(io: Io, file: Io.File, bytes: []const u8, draining: bool) !void {
    var transfer: Transfer = .{ .io = io, .file = file, .bytes = bytes };
    var worker = try io.concurrent(Transfer.perform, .{&transfer});
    defer worker.cancel(io);
    const cancelled = try delivery.wait(&transfer.done, io, draining);
    if (cancelled) io.recancel();
    try transfer.result.?;
}

const Transfer = struct {
    io: Io,
    file: Io.File,
    bytes: []const u8,
    done: Io.Event = .unset,
    result: ?anyerror!void = null,

    fn perform(self: *Transfer) void {
        defer self.done.set(self.io);
        self.result = self.file.writeStreamingAll(self.io, self.bytes);
    }
};
