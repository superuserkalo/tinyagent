const std = @import("std");
const Agent = @import("agent.zig").Agent;
const Sessions = @import("http_sessions.zig").Sessions;
const Connection = @import("http_connection.zig").Connection;
const Io = std.Io;

pub const Options = struct {
    host: []const u8 = "127.0.0.1",
    port: u16 = 8080,
    token: ?[]const u8 = null,
    allow_remote: bool = false,
};

/// Owns a listener, sessions, and every connection task. Agent configuration
/// stays borrowed. Use a thread-safe allocator and keep the Io alive throughout.
pub const HttpServer = struct {
    allocator: std.mem.Allocator,
    io: Io,
    listener: Io.net.Server,
    sessions: Sessions,
    authorization: ?[32]u8,
    url: []u8,
    stopping: Io.Event = .unset,
    listener_closed: bool = false,
    worker: ?Io.Future(anyerror!void) = null,

    pub fn start(allocator: std.mem.Allocator, io: Io, agent: Agent, options: Options) !*HttpServer {
        const address = try Io.net.IpAddress.parse(options.host, options.port);
        if (options.token) |token| {
            if (token.len == 0) return error.InvalidToken;
            for (token) |byte| if (byte < 0x21 or byte > 0x7e) return error.InvalidToken;
        }
        const loopback = switch (address) {
            .ip4 => |ip| ip.bytes[0] == 127,
            .ip6 => |ip| std.mem.allEqual(u8, ip.bytes[0..15], 0) and ip.bytes[15] == 1,
        };
        if (!loopback and (!options.allow_remote or options.token == null)) return error.RemoteExposureNotAllowed;
        var listener = try address.listen(io, .{ .reuse_address = true });
        errdefer listener.deinit(io);
        const server = try allocator.create(HttpServer);
        errdefer allocator.destroy(server);
        var authorization: ?[32]u8 = null;
        if (options.token) |token| {
            var hash = std.crypto.hash.sha2.Sha256.init(.{});
            hash.update("Bearer ");
            hash.update(token);
            var digest: [32]u8 = undefined;
            hash.final(&digest);
            authorization = digest;
        }
        const url = try std.fmt.allocPrint(allocator, "http://{f}", .{listener.socket.address});
        errdefer allocator.free(url);
        server.* = .{
            .allocator = allocator,
            .io = io,
            .listener = listener,
            .sessions = .{ .allocator = allocator, .io = io, .agent = agent },
            .authorization = authorization,
            .url = url,
        };
        server.worker = try io.concurrent(HttpServer.serve, .{server});
        return server;
    }

    /// Request shutdown without joining. Safe from provider/event callbacks.
    pub fn stop(self: *HttpServer) void {
        self.sessions.stop();
        self.stopping.set(self.io);
    }

    /// One external owner manages wait/deinit. Never wait from a server callback.
    pub fn wait(self: *HttpServer) !void {
        try self.worker.?.await(self.io);
    }

    pub fn deinit(self: *HttpServer) void {
        self.stop();
        self.wait() catch {};
        self.sessions.deinit();
        if (!self.listener_closed) self.listener.deinit(self.io);
        self.allocator.free(self.url);
        self.allocator.destroy(self);
    }

    fn serve(self: *HttpServer) anyerror!void {
        defer {
            self.stop();
            self.listener.deinit(self.io);
            self.listener_closed = true;
        }
        const Outcome = union(enum) { accept: anyerror!void, stopped: Io.Cancelable!void };
        var outcomes: [2]Outcome = undefined;
        var select: Io.Select(Outcome) = .init(self.io, &outcomes);
        defer select.cancelDiscard();
        try select.concurrent(.accept, HttpServer.accept, .{self});
        try select.concurrent(.stopped, Io.Event.wait, .{ &self.stopping, self.io });
        const result = select.await() catch Outcome{ .stopped = error.Canceled };
        self.stop();
        select.cancelDiscard();
        if (result == .accept) result.accept catch |err| switch (err) {
            error.Canceled => {},
            else => return err,
        };
    }

    fn accept(self: *HttpServer) !void {
        var connections: Io.Group = .init;
        defer connections.cancel(self.io);
        while (true) {
            const stream = self.listener.accept(self.io) catch |err| switch (err) {
                error.ConnectionAborted => continue,
                else => return err,
            };
            connections.concurrent(self.io, Connection.serve, .{ self, stream }) catch |err| {
                stream.close(self.io);
                return err;
            };
        }
    }
};
