const std = @import("std");
const sdk = @import("tinyagent");
const testing = std.testing;
const allocator = testing.allocator;
const io = testing.io;

const Fixture = struct {
    mode: enum { text, fail, wait } = .text,
    entered: std.Io.Event = .unset,
    cleanup: std.Io.Event = .unset,
    release: std.Io.Event = .unset,
    hold_cleanup: bool = false,
    calls: usize = 0,
    history_count: usize = 0,

    fn agent(self: *Fixture) sdk.Agent {
        return .{ .provider = .{ .context = self, .stream = stream }, .model = "offline" };
    }

    fn stream(context: ?*anyopaque, memory: std.mem.Allocator, task_io: std.Io, request: sdk.ProviderRequest, text: sdk.TextHandler) !sdk.ProviderTurn {
        const self: *Fixture = @ptrCast(@alignCast(context.?));
        self.calls += 1;
        self.history_count = request.history.len;
        if (request.history.len > 1) {
            try testing.expectEqualStrings("first", request.history[0].user);
            try testing.expectEqualStrings("answer:first", request.history[1].assistant.text);
        }
        defer {
            self.cleanup.set(task_io);
            if (self.hold_cleanup) self.release.waitUncancelable(task_io);
        }
        const prompt = request.history[request.history.len - 1].user;
        try text.emit(task_io, prompt);
        self.entered.set(task_io);
        if (self.mode == .fail) return error.ProviderUnavailable;
        if (self.mode == .wait) {
            var forever: std.Io.Event = .unset;
            try forever.wait(task_io);
        }
        return .{ .assistant = .{ .text = try std.fmt.allocPrint(memory, "answer:{s}", .{prompt}) } };
    }
};

const Events = struct {
    count: usize = 0,
    terminal_count: usize = 0,
    status: enum { none, completed, failed, aborted } = .none,
    id: ?sdk.Session.RunId = null,
    fail_terminal: bool = false,
    hold_terminal: bool = false,
    terminal_entered: std.Io.Event = .unset,
    release: std.Io.Event = .unset,

    fn handler(self: *Events) sdk.EventHandler {
        return .{ .context = self, .callback = emit };
    }

    fn emit(context: ?*anyopaque, task_io: std.Io, event: sdk.Event) !void {
        const self: *Events = @ptrCast(@alignCast(context.?));
        self.count += 1;
        try testing.expectEqual(self.count, event.sequence);
        try testing.expectEqual(34, event.run_id.len);
        if (self.id) |id| try testing.expectEqualStrings(&id, event.run_id) else self.id = event.run_id[0..34].*;
        if (self.count == 1) try testing.expect(event.kind == .agent_start);
        switch (event.kind) {
            .completed, .failed, .aborted => {
                self.terminal_count += 1;
                self.status = switch (event.kind) {
                    .completed => .completed,
                    .failed => .failed,
                    .aborted => .aborted,
                    else => unreachable,
                };
                self.terminal_entered.set(task_io);
                if (self.hold_terminal) try self.release.wait(task_io);
                if (self.fail_terminal) return error.ConsumerStopped;
            },
            else => {},
        }
    }
};

fn discard(future: *std.Io.Future(anyerror!sdk.RunResult)) void {
    if (future.cancel(io)) |value| {
        var result = value;
        result.deinit();
    } else |_| {}
}

test "session history survives returned result mutation and deinitialization" {
    var fixture: Fixture = .{};
    var session = sdk.Session.init(allocator, io, fixture.agent());
    defer session.deinit();
    var first = try session.run("first", .{});
    const first_id = first.run_id[0..34].*;
    try testing.expectEqual(2, session.state().message_count);
    @constCast(first.history[0].user)[0] = 'X';
    @constCast(first.history[1].assistant.text)[0] = 'X';
    first.deinit();
    var second = try session.run("next", .{});
    defer second.deinit();
    try testing.expectEqual(3, fixture.history_count);
    try testing.expectEqual(4, session.state().message_count);
    try testing.expectEqualStrings("answer:next", second.text);
    try testing.expect(!std.mem.eql(u8, &first_id, second.run_id));
    try testing.expect(session.state().run_id == null);
    try testing.expect(session.cancel() == null);
}

const Acknowledgement = struct {
    entered: std.Io.Event = .unset,
    release: std.Io.Event = .unset,
    id: sdk.Session.RunId = undefined,
    fail: bool = false,

    fn send(context: ?*anyopaque, task_io: std.Io, id: []const u8) !void {
        const self: *Acknowledgement = @ptrCast(@alignCast(context.?));
        self.id = id[0..34].*;
        self.entered.set(task_io);
        try self.release.wait(task_io);
        if (self.fail) return error.AcknowledgementFailed;
    }
};

test "prompt acknowledgement precedes events and reserves the session" {
    var fixture: Fixture = .{};
    var events: Events = .{};
    var ack: Acknowledgement = .{};
    var session = sdk.Session.init(allocator, io, fixture.agent());
    defer session.deinit();
    var future = try io.concurrent(sdk.Session.run, .{ &session, "first", sdk.Session.Options{
        .before_run = .{ .context = &ack, .callback = Acknowledgement.send },
        .on_event = events.handler(),
    } });
    defer {
        ack.release.set(io);
        discard(&future);
    }
    try ack.entered.wait(io);
    try testing.expectEqual(0, fixture.calls);
    try testing.expectEqual(0, events.count);
    try testing.expectEqual(ack.id, session.state().run_id.?);
    try testing.expectError(error.SessionBusy, session.run("overlap", .{}));
    ack.release.set(io);
    _ = try future.await(io);
    try testing.expectEqual(ack.id, events.id.?);
    try testing.expectEqual(.completed, events.status);
    try testing.expectEqual(1, events.terminal_count);
}

test "acknowledgement, provider, and terminal consumer failures preserve history" {
    var fixture: Fixture = .{};
    var session = sdk.Session.init(allocator, io, fixture.agent());
    defer session.deinit();
    var first = try session.run("first", .{});
    defer first.deinit();
    var ack: Acknowledgement = .{ .fail = true };
    ack.release.set(io);
    var events: Events = .{};
    try testing.expectError(error.AcknowledgementFailed, session.run("next", .{
        .before_run = .{ .context = &ack, .callback = Acknowledgement.send },
        .on_event = events.handler(),
    }));
    try testing.expectEqual(0, events.count);
    try testing.expectEqual(1, fixture.calls);
    fixture.mode = .fail;
    try testing.expectError(error.ProviderUnavailable, session.run("next", .{ .on_event = events.handler() }));
    try testing.expectEqual(.failed, events.status);
    try testing.expectEqual(2, session.state().message_count);
    fixture.mode = .text;
    events = .{ .fail_terminal = true };
    try testing.expectError(error.ConsumerStopped, session.run("next", .{ .on_event = events.handler() }));
    try testing.expectEqual(1, events.terminal_count);
    try testing.expectEqual(2, session.state().message_count);
    var next = try session.run("next", .{});
    defer next.deinit();
    try testing.expectEqual(4, session.state().message_count);
}

test "cancellation returns promptly and keeps admission until held cleanup finishes" {
    var fixture: Fixture = .{ .mode = .wait, .hold_cleanup = true };
    var events: Events = .{};
    var session = sdk.Session.init(allocator, io, fixture.agent());
    defer session.deinit();
    var future = try io.concurrent(sdk.Session.run, .{ &session, "first", sdk.Session.Options{ .on_event = events.handler() } });
    defer {
        fixture.release.set(io);
        discard(&future);
    }
    try fixture.entered.wait(io);
    const id = session.cancel().?;
    try fixture.cleanup.wait(io);
    try testing.expectEqual(id, session.cancel().?);
    try testing.expectEqual(id, session.state().run_id.?);
    try testing.expectError(error.SessionBusy, session.run("overlap", .{}));
    try testing.expectEqual(0, events.terminal_count);
    fixture.release.set(io);
    session.waitIdle(id);
    try testing.expectError(error.Canceled, future.await(io));
    try testing.expectEqual(.aborted, events.status);
    try testing.expectEqual(1, events.terminal_count);
    try testing.expect(session.cancel() == null);
}

test "cancellation during final delivery cannot commit or emit a second terminal" {
    var fixture: Fixture = .{};
    var events: Events = .{ .hold_terminal = true };
    var session = sdk.Session.init(allocator, io, fixture.agent());
    defer session.deinit();
    var future = try io.concurrent(sdk.Session.run, .{ &session, "first", sdk.Session.Options{ .on_event = events.handler() } });
    defer {
        events.release.set(io);
        discard(&future);
    }
    try events.terminal_entered.wait(io);
    try testing.expectEqual(0, session.state().message_count);
    const id = session.cancel().?;
    session.waitIdle(id);
    try testing.expectError(error.Canceled, future.await(io));
    try testing.expectEqual(1, events.terminal_count);
    try testing.expectEqual(0, session.state().message_count);
}

test "reset hides history immediately and keeps its active run reserved during cleanup" {
    var fixture: Fixture = .{};
    var session = sdk.Session.init(allocator, io, fixture.agent());
    defer session.deinit();
    var first = try session.run("first", .{});
    defer first.deinit();
    fixture = .{ .mode = .wait, .hold_cleanup = true };
    var future = try io.concurrent(sdk.Session.run, .{ &session, "next", sdk.Session.Options{} });
    defer {
        fixture.release.set(io);
        discard(&future);
    }
    try fixture.entered.wait(io);
    const id = (try session.reset()).?;
    try fixture.cleanup.wait(io);
    try testing.expectEqual(0, session.state().message_count);
    try testing.expectEqual(id, session.state().run_id.?);
    try testing.expectError(error.SessionBusy, session.run("overlap", .{}));
    fixture.release.set(io);
    try testing.expectError(error.Canceled, future.await(io));
    fixture = .{};
    var next = try session.run("fresh", .{});
    defer next.deinit();
    try testing.expectEqual(1, fixture.history_count);
    try testing.expect(try session.reset() == null);
    try testing.expectEqual(0, session.state().message_count);
}

test "caller cancellation joins native work and leaves its session reusable" {
    var fixture: Fixture = .{ .mode = .wait };
    var events: Events = .{};
    var session = sdk.Session.init(allocator, io, fixture.agent());
    defer session.deinit();
    var future = try io.concurrent(sdk.Session.run, .{ &session, "first", sdk.Session.Options{ .on_event = events.handler() } });
    defer discard(&future);
    try fixture.entered.wait(io);
    try testing.expectError(error.Canceled, future.cancel(io));
    try testing.expect(fixture.cleanup.isSet());
    try testing.expectEqual(.aborted, events.status);
    try testing.expectEqual(0, session.state().message_count);
    fixture = .{};
    var next = try session.run("fresh", .{});
    defer next.deinit();
}

test "waiting for an old run does not join a newer run" {
    var fixture: Fixture = .{};
    var session = sdk.Session.init(allocator, io, fixture.agent());
    defer session.deinit();
    var first = try session.run("first", .{});
    defer first.deinit();
    const old_id = first.run_id[0..34].*;
    fixture = .{ .mode = .wait };
    var future = try io.concurrent(sdk.Session.run, .{ &session, "next", sdk.Session.Options{} });
    defer discard(&future);
    try fixture.entered.wait(io);
    session.waitIdle(old_id);
    const active = session.state().run_id.?;
    try testing.expect(!std.mem.eql(u8, &old_id, &active));
    _ = session.cancel();
    session.waitIdle(active);
    try testing.expectError(error.Canceled, future.await(io));
}

test "invalid prompts and closed sessions are rejected before acknowledgement" {
    var fixture: Fixture = .{};
    var session = sdk.Session.init(allocator, io, fixture.agent());
    defer session.deinit();
    var ack: Acknowledgement = .{};
    const options: sdk.Session.Options = .{ .before_run = .{ .context = &ack, .callback = Acknowledgement.send } };
    try testing.expectError(error.InvalidUtf8, session.run("\xff", options));
    try testing.expect(!ack.entered.isSet());
    try testing.expect(session.stop() == null);
    try testing.expect(session.state().closed);
    try testing.expectError(error.SessionClosed, session.run("first", options));
    try testing.expectError(error.SessionClosed, session.reset());
    try testing.expectEqual(0, fixture.calls);
}

fn allocationRun(memory: std.mem.Allocator) !void {
    var fixture: Fixture = .{};
    var session = sdk.Session.init(memory, io, fixture.agent());
    defer session.deinit();
    var first = try session.run("first", .{});
    defer first.deinit();
    var second = try session.run("next", .{});
    defer second.deinit();
}

test "allocation failure releases active work and partially copied history" {
    try testing.checkAllAllocationFailures(allocator, allocationRun, .{});
}

const SharedProvider = struct {
    first_entered: std.Io.Event = .unset,
    second_entered: std.Io.Event = .unset,
    release_second: std.Io.Event = .unset,

    fn stream(context: ?*anyopaque, _: std.mem.Allocator, task_io: std.Io, request: sdk.ProviderRequest, _: sdk.TextHandler) !sdk.ProviderTurn {
        const self: *SharedProvider = @ptrCast(@alignCast(context.?));
        const prompt = request.history[request.history.len - 1].user;
        if (std.mem.eql(u8, prompt, "wait:first")) {
            try testing.expectEqual(3, request.history.len);
            try testing.expectEqualStrings("seed:first", request.history[0].user);
            self.first_entered.set(task_io);
            var forever: std.Io.Event = .unset;
            try forever.wait(task_io);
        } else if (std.mem.eql(u8, prompt, "wait:second")) {
            try testing.expectEqual(1, request.history.len);
            self.second_entered.set(task_io);
            try self.release_second.wait(task_io);
        }
        return .{ .assistant = .{ .text = prompt } };
    }
};

test "sessions share one agent while keeping concurrent history and cancellation independent" {
    var provider: SharedProvider = .{};
    const agent: sdk.Agent = .{ .provider = .{ .context = &provider, .stream = SharedProvider.stream }, .model = "offline" };
    var first_session = sdk.Session.init(allocator, io, agent);
    defer first_session.deinit();
    var second_session = sdk.Session.init(allocator, io, agent);
    defer second_session.deinit();
    var seed = try first_session.run("seed:first", .{});
    defer seed.deinit();
    var first = try io.concurrent(sdk.Session.run, .{ &first_session, "wait:first", sdk.Session.Options{} });
    defer discard(&first);
    var second = try io.concurrent(sdk.Session.run, .{ &second_session, "wait:second", sdk.Session.Options{} });
    defer {
        provider.release_second.set(io);
        discard(&second);
    }
    try provider.first_entered.wait(io);
    try provider.second_entered.wait(io);
    const second_id = second_session.state().run_id.?;
    _ = first_session.cancel();
    try testing.expectError(error.Canceled, first.await(io));
    try testing.expectEqual(2, first_session.state().message_count);
    try testing.expectEqual(second_id, second_session.state().run_id.?);
    provider.release_second.set(io);
    const result = try second.await(io);
    try testing.expectEqual(2, result.history.len);
    try testing.expectEqualStrings("wait:second", result.history[0].user);
}

const Shutdown = struct {
    done: std.atomic.Value(bool) = .init(false),

    fn close(self: *Shutdown, session: *sdk.Session) void {
        session.deinit();
        self.done.store(true, .release);
    }
};

test "external deinit closes admission and joins held provider cleanup" {
    var fixture: Fixture = .{ .mode = .wait, .hold_cleanup = true };
    var session = sdk.Session.init(allocator, io, fixture.agent());
    var shutdown_started = false;
    defer if (!shutdown_started) session.deinit();
    var running = try io.concurrent(sdk.Session.run, .{ &session, "first", sdk.Session.Options{} });
    defer {
        fixture.release.set(io);
        discard(&running);
    }
    try fixture.entered.wait(io);
    var shutdown: Shutdown = .{};
    var closing = try io.concurrent(Shutdown.close, .{ &shutdown, &session });
    shutdown_started = true;
    defer {
        fixture.release.set(io);
        closing.cancel(io);
    }
    try fixture.cleanup.wait(io);
    try testing.expect(session.state().closed);
    try testing.expect(!shutdown.done.load(.acquire));
    try testing.expectError(error.SessionClosed, session.run("overlap", .{}));
    fixture.release.set(io);
    closing.await(io);
    try testing.expect(shutdown.done.load(.acquire));
    try testing.expectError(error.Canceled, running.await(io));
}

const CallbackStop = struct {
    session: *sdk.Session,
    events: Events = .{},

    fn emit(context: ?*anyopaque, task_io: std.Io, event: sdk.Event) !void {
        const self: *CallbackStop = @ptrCast(@alignCast(context.?));
        try self.events.handler().emit(task_io, event);
        if (event.kind == .agent_start) _ = self.session.stop();
    }
};

test "a callback can stop its session without waiting on itself" {
    var fixture: Fixture = .{};
    var session = sdk.Session.init(allocator, io, fixture.agent());
    defer session.deinit();
    var callback: CallbackStop = .{ .session = &session };
    try testing.expectError(error.Canceled, session.run("first", .{
        .on_event = .{ .context = &callback, .callback = CallbackStop.emit },
    }));
    try testing.expect(session.state().closed);
    try testing.expectEqual(0, session.state().message_count);
    try testing.expectEqual(1, callback.events.terminal_count);
    try testing.expectEqual(.aborted, callback.events.status);
}
