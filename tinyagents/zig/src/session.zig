const std = @import("std");
const core = @import("agent.zig");
const types = @import("types.zig");
const history = @import("history.zig");
const Io = std.Io;
const Allocator = std.mem.Allocator;

/// Owns committed history and permits one run at a time. Keep this value at a
/// stable address, using a thread-safe allocator for concurrent callers. Agent
/// configuration and provider lifetime remain borrowed from the caller.
pub const Session = struct {
    allocator: Allocator,
    io: Io,
    agent: core.Agent,
    mutex: Io.Mutex = .init,
    changed: Io.Condition = .init,
    committed: ?History = null,
    active: ?*Run = null,
    resetting: bool = false,
    closed: bool = false,

    pub const RunId = core.RunId;
    pub const State = struct {
        run_id: ?RunId,
        message_count: usize,
        closed: bool,
    };
    pub const Options = struct {
        /// Runs before agent events, on the same owned task as Agent.run.
        /// A transport can finish its prompt acknowledgement here.
        before_run: ?struct {
            context: ?*anyopaque = null,
            callback: *const fn (?*anyopaque, Io, []const u8) anyerror!void,
        } = null,
        on_event: ?types.EventHandler = null,
    };

    pub fn init(allocator: Allocator, io: Io, agent: core.Agent) Session {
        return .{ .allocator = allocator, .io = io, .agent = agent };
    }

    /// External owner only: close admission, cancel, and wait for the active
    /// call's cleanup. Stop other callers before deinitializing the session.
    /// A provider or event callback uses stop instead of waiting on itself.
    pub fn deinit(self: *Session) void {
        _ = self.stop();
        self.waitIdle(null);
        if (self.committed) |*value| value.deinit();
        self.* = undefined;
    }

    /// Synchronous, like Agent.run. Use io.concurrent to keep a transport's
    /// reader responsive. The returned result and committed history have
    /// independent ownership; the caller must deinit the returned result.
    pub fn run(self: *Session, prompt: []const u8, options: Options) anyerror!core.RunResult {
        var active: Run = .{};
        {
            try self.mutex.lock(self.io);
            defer self.mutex.unlock(self.io);
            if (self.closed) return error.SessionClosed;
            if (self.active != null or self.resetting) return error.SessionBusy;
            if (!std.unicode.utf8ValidateSlice(prompt)) return error.InvalidUtf8;
            active.id = try core.newRunId(self.io);
            self.active = &active;
        }

        var result = core.runControlled(self.io, &active.cancelled, Run.call, .{ &active, self, prompt, options }) catch |err| {
            _ = self.settle(&active, null);
            return err;
        };
        errdefer result.deinit();
        const saved = History.copy(self.allocator, result.history) catch |err| {
            _ = self.settle(&active, null);
            return err;
        };
        self.io.checkCancel() catch active.cancelled.set(self.io);
        if (!self.settle(&active, saved)) return error.Canceled;
        return result;
    }

    pub fn state(self: *Session) State {
        self.mutex.lockUncancelable(self.io);
        defer self.mutex.unlock(self.io);
        return .{
            .run_id = if (self.active) |active| active.id else null,
            .message_count = if (!self.resetting and self.committed != null) self.committed.?.messages.len else 0,
            .closed = self.closed,
        };
    }

    /// Request cancellation without waiting for cooperative cleanup. Safe in
    /// callbacks. A returned ID identifies the run that was active at this call.
    pub fn cancel(self: *Session) ?RunId {
        self.mutex.lockUncancelable(self.io);
        defer self.mutex.unlock(self.io);
        return self.cancelLocked();
    }

    /// Clear visible history and request cancellation. Admission stays closed
    /// until that run finishes. Call waitIdle from an external owner to join it.
    pub fn reset(self: *Session) !?RunId {
        self.mutex.lockUncancelable(self.io);
        defer self.mutex.unlock(self.io);
        if (self.closed) return error.SessionClosed;
        if (self.active != null) {
            self.resetting = true;
            return self.cancelLocked();
        }
        self.clearHistory();
        return null;
    }

    /// Permanently close admission and request cancellation. Safe in callbacks.
    pub fn stop(self: *Session) ?RunId {
        self.mutex.lockUncancelable(self.io);
        defer self.mutex.unlock(self.io);
        self.closed = true;
        return self.cancelLocked();
    }

    /// Join cooperative cleanup even if this caller is cancelled. An expected
    /// ID prevents an old transport from waiting on a later run. Never call
    /// from this session's provider or callbacks; use cancel, reset, or stop.
    pub fn waitIdle(self: *Session, expected: ?RunId) void {
        self.mutex.lockUncancelable(self.io);
        defer self.mutex.unlock(self.io);
        while (self.active) |active| {
            if (expected) |id| {
                if (!std.mem.eql(u8, &id, &active.id)) return;
            }
            self.changed.waitUncancelable(self.io, &self.mutex);
        }
    }

    fn cancelLocked(self: *Session) ?RunId {
        const active = self.active orelse return null;
        active.cancelled.set(self.io);
        return active.id;
    }

    fn clearHistory(self: *Session) void {
        if (self.committed) |*value| value.deinit();
        self.committed = null;
    }

    // Consume the new snapshot, committing it only while this run still owns
    // admission. Reset retains old storage until the worker stops borrowing it.
    fn settle(self: *Session, active: *Run, saved: ?History) bool {
        self.mutex.lockUncancelable(self.io);
        defer self.mutex.unlock(self.io);
        std.debug.assert(self.active == active);
        const commit = saved != null and !active.cancelled.isSet() and !self.closed and !self.resetting;
        if (commit) {
            self.clearHistory();
            self.committed = saved;
        } else if (saved) |value| {
            var discarded = value;
            discarded.deinit();
        }
        if (self.resetting) self.clearHistory();
        self.resetting = false;
        self.active = null;
        self.changed.broadcast(self.io);
        return commit;
    }
};

const Run = struct {
    id: Session.RunId = undefined,
    cancelled: Io.Event = .unset,

    fn call(self: *Run, session: *Session, prompt: []const u8, options: Session.Options) anyerror!core.RunResult {
        try session.io.checkCancel();
        if (self.cancelled.isSet()) return error.Canceled;
        if (options.before_run) |handler| try handler.callback(handler.context, session.io, &self.id);
        return core.runInScope(session.agent, session.allocator, session.io, prompt, .{
            .run_id = &self.id,
            .history = if (session.committed) |saved| saved.messages else &.{},
            .on_event = options.on_event,
            .cancellation = &self.cancelled,
        });
    }
};

const History = struct {
    arena: std.heap.ArenaAllocator,
    messages: []const types.HistoryMessage,

    fn copy(allocator: Allocator, messages: []const types.HistoryMessage) !History {
        var arena = std.heap.ArenaAllocator.init(allocator);
        errdefer arena.deinit();
        const memory = arena.allocator();
        const copies = try memory.alloc(types.HistoryMessage, messages.len);
        for (messages, copies) |item, *value| value.* = try history.message(memory, item);
        return .{ .arena = arena, .messages = copies };
    }

    fn deinit(self: *History) void {
        self.arena.deinit();
        self.* = undefined;
    }
};
