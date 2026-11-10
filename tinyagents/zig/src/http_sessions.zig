const std = @import("std");
const Agent = @import("agent.zig").Agent;
const Session = @import("session.zig").Session;
const Io = std.Io;

pub const Entry = struct {
    id: [34]u8,
    session: Session,
    references: usize = 1,
};

/// The map owns one reference per visible session. Requests borrow references,
/// so deletion can withdraw a session while earlier requests finish cleanup.
pub const Sessions = struct {
    allocator: std.mem.Allocator,
    io: Io,
    agent: Agent,
    mutex: Io.Mutex = .init,
    entries: std.StringHashMapUnmanaged(*Entry) = .empty,
    closed: bool = false,

    pub fn create(self: *Sessions) !*Entry {
        const entry = try self.allocator.create(Entry);
        errdefer self.allocator.destroy(entry);
        var random: [16]u8 = undefined;
        try self.io.randomSecure(&random);
        entry.* = .{ .id = undefined, .session = Session.init(self.allocator, self.io, self.agent) };
        _ = try std.fmt.bufPrint(&entry.id, "s_{x}", .{&random});
        self.mutex.lockUncancelable(self.io);
        defer self.mutex.unlock(self.io);
        if (self.closed) return error.ServerClosed;
        const result = try self.entries.getOrPut(self.allocator, &entry.id);
        if (result.found_existing) return error.SessionIdCollision;
        result.value_ptr.* = entry;
        entry.references += 1;
        return entry;
    }

    pub fn acquire(self: *Sessions, id: []const u8) ?*Entry {
        self.mutex.lockUncancelable(self.io);
        defer self.mutex.unlock(self.io);
        if (self.closed) return null;
        const entry = self.entries.get(id) orelse return null;
        entry.references += 1;
        return entry;
    }

    pub fn remove(self: *Sessions, id: []const u8) ?*Entry {
        self.mutex.lockUncancelable(self.io);
        defer self.mutex.unlock(self.io);
        const removed = self.entries.fetchRemove(id) orelse return null;
        _ = removed.value.session.stop();
        // Transfer the map's reference to the deleting request. The server's
        // connection group also joins that request during shutdown.
        return removed.value;
    }

    pub fn release(self: *Sessions, entry: *Entry) void {
        self.mutex.lockUncancelable(self.io);
        entry.references -= 1;
        const last = entry.references == 0;
        self.mutex.unlock(self.io);
        if (last) {
            entry.session.deinit();
            self.allocator.destroy(entry);
        }
    }

    pub fn stop(self: *Sessions) void {
        self.mutex.lockUncancelable(self.io);
        defer self.mutex.unlock(self.io);
        self.closed = true;
        var iterator = self.entries.valueIterator();
        while (iterator.next()) |entry| _ = entry.*.session.stop();
    }

    /// Call only after the server has joined every request.
    pub fn deinit(self: *Sessions) void {
        var iterator = self.entries.valueIterator();
        while (iterator.next()) |entry| {
            std.debug.assert(entry.*.references == 1);
            entry.*.session.deinit();
            self.allocator.destroy(entry.*);
        }
        self.entries.deinit(self.allocator);
        self.* = undefined;
    }
};
