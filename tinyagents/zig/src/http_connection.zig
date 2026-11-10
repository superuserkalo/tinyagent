const std = @import("std");
const HttpServer = @import("http.zig").HttpServer;
const Request = @import("http_request.zig");
const Prompt = @import("http_stream.zig").Prompt;
const jsonl = @import("jsonl.zig");
const Io = std.Io;

pub const Connection = struct {
    server: *HttpServer,
    stream: Io.net.Stream,
    started: bool = false,

    pub fn serve(server: *HttpServer, stream: Io.net.Stream) Io.Cancelable!void {
        defer stream.close(server.io);
        var connection: Connection = .{ .server = server, .stream = stream };
        connection.handle() catch |err| {
            if (!connection.started and err != error.Canceled) connection.failure(err) catch {};
        };
    }

    fn handle(self: *Connection) !void {
        var read_buffer: [16 * 1024]u8 = undefined;
        var reader = self.stream.reader(self.server.io, &read_buffer);
        var writer = self.stream.writer(self.server.io, &.{});
        var parser = std.http.Server.init(&reader.interface, &writer.interface);
        var request = parser.receiveHead() catch {
            if (reader.err) |err| if (err == error.Canceled) return error.Canceled;
            return error.InvalidRequest;
        };
        self.route(&request) catch |err| {
            // The HTTP parser wraps native cancellation in ReadFailed or
            // WriteFailed. Preserve it instead of sending an error response.
            if (reader.err) |cause| if (cause == error.Canceled) return error.Canceled;
            if (writer.err) |cause| if (cause == error.Canceled) return error.Canceled;
            return err;
        };
    }

    fn route(self: *Connection, request: *std.http.Server.Request) !void {
        try Request.validate(request, self.server.authorization);
        const target = request.head.target;
        const path = target[0 .. std.mem.indexOfScalar(u8, target, '?') orelse target.len];
        const method = request.head.method;
        if (method == .GET and std.mem.eql(u8, path, "/health")) {
            return self.respond(.ok, .{ .status = "ok", .protocolVersion = 1 });
        }
        if (method == .POST and std.mem.eql(u8, path, "/sessions")) {
            const body = try Request.readObject(request, self.server.allocator, true);
            defer if (body) |value| value.deinit();
            const entry = try self.server.sessions.create();
            defer self.server.sessions.release(entry);
            return self.respond(.created, .{ .sessionId = @as([]const u8, &entry.id) });
        }
        if (!std.mem.startsWith(u8, path, "/sessions/")) return error.RouteNotFound;
        var parts = std.mem.splitScalar(u8, path[10..], '/');
        const id = parts.first();
        const action = parts.next();
        if (id.len == 0 or parts.next() != null) return error.RouteNotFound;
        if (method == .DELETE and action == null) {
            const entry = self.server.sessions.remove(id) orelse return error.SessionNotFound;
            defer self.server.sessions.release(entry);
            entry.session.waitIdle(null);
            return self.empty();
        }
        if (method != .POST or action == null) return error.RouteNotFound;
        const prompt = std.mem.eql(u8, action.?, "prompts");
        const cancel = std.mem.eql(u8, action.?, "cancel");
        if (!prompt and !cancel) return error.RouteNotFound;
        const entry = self.server.sessions.acquire(id) orelse return error.SessionNotFound;
        defer self.server.sessions.release(entry);
        const body = try Request.readObject(request, self.server.allocator, cancel);
        defer if (body) |value| value.deinit();
        if (cancel) {
            // Read the request body before deciding which run is active.
            if (entry.session.state().closed) return error.SessionClosed;
            const run_id = entry.session.cancel();
            return self.respond(.ok, .{ .accepted = run_id != null, .runId = @as(?[]const u8, if (run_id) |*value| value else null) });
        }
        const message = body.?.value.object.get("message") orelse return error.InvalidPrompt;
        if (message != .string or message.string.len == 0) return error.InvalidPrompt;
        var response: Prompt = .{ .allocator = self.server.allocator, .io = self.server.io, .stream = self.stream, .session = &entry.session };
        const result = response.run(message.string);
        self.started = response.started;
        try result;
    }

    fn respond(self: *Connection, status: std.http.Status, value: anytype) !void {
        const body = try jsonl.encode(self.server.allocator, value, .{});
        defer self.server.allocator.free(body);
        self.started = true;
        var writer = self.stream.writer(self.server.io, &.{});
        try writer.interface.print("HTTP/1.1 {d} {s}\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: {d}\r\nCache-Control: no-store\r\nConnection: close\r\n", .{
            @intFromEnum(status), status.phrase().?, body.len,
        });
        if (status == .unauthorized) try writer.interface.writeAll("WWW-Authenticate: Bearer\r\n");
        try writer.interface.writeAll("\r\n");
        try writer.interface.writeAll(body);
    }

    fn empty(self: *Connection) !void {
        self.started = true;
        var writer = self.stream.writer(self.server.io, &.{});
        try writer.interface.writeAll("HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n");
    }

    fn failure(self: *Connection, err: anyerror) !void {
        const detail: struct { status: std.http.Status, code: []const u8, message: []const u8 } = switch (err) {
            error.Unauthorized => .{ .status = .unauthorized, .code = "unauthorized", .message = "Missing or invalid bearer token" },
            error.RouteNotFound => .{ .status = .not_found, .code = "not_found", .message = "Unknown route" },
            error.SessionNotFound, error.SessionClosed => .{ .status = .not_found, .code = "session_not_found", .message = "Unknown session" },
            error.SessionBusy => .{ .status = .conflict, .code = "session_busy", .message = "The session already has an active run" },
            error.BodyTooLarge => .{ .status = .payload_too_large, .code = "body_too_large", .message = "Request body exceeds 1048576 UTF-8 bytes" },
            error.UnsupportedMediaType => .{ .status = .unsupported_media_type, .code = "unsupported_media_type", .message = "Expected application/json with UTF-8 encoding" },
            error.InvalidUtf8 => .{ .status = .bad_request, .code = "invalid_utf8", .message = "Request body is not valid UTF-8" },
            error.InvalidPrompt => .{ .status = .bad_request, .code = "invalid_request", .message = "message must be a non-empty string" },
            error.InvalidRequest => .{ .status = .bad_request, .code = "invalid_request", .message = "Malformed HTTP request or JSON object" },
            error.ServerClosed => .{ .status = .service_unavailable, .code = "server_closed", .message = "Server is shutting down" },
            else => .{ .status = .internal_server_error, .code = "internal_error", .message = "Request failed before streaming" },
        };
        try self.respond(detail.status, .{ .@"error" = .{
            .code = detail.code,
            .message = detail.message,
            .retryable = detail.status == .conflict or @intFromEnum(detail.status) >= 500,
        } });
    }
};
