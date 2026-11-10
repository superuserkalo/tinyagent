const std = @import("std");
const max_body_bytes = 1_048_576;

/// Validate the header syntax and ambiguity that the standard HTTP parser
/// leaves to its caller. All borrowed strings expire when body reading starts.
pub fn validate(request: *std.http.Server.Request, authorization_digest: ?[32]u8) !void {
    var lines = std.mem.splitSequence(u8, request.head_buffer, "\r\n");
    _ = lines.next();
    var authorization: ?[]const u8 = null;
    var authorization_count: usize = 0;
    var content_types: usize = 0;
    var transfers: usize = 0;
    while (lines.next()) |line| {
        if (line.len == 0) break;
        const colon = std.mem.indexOfScalar(u8, line, ':') orelse return error.InvalidRequest;
        const name = line[0..colon];
        if (name.len == 0) return error.InvalidRequest;
        for (name) |byte| {
            if (!std.ascii.isAlphanumeric(byte) and std.mem.indexOfScalar(u8, "!#$%&'*+-.^_`|~", byte) == null) return error.InvalidRequest;
        }
        const value = std.mem.trim(u8, line[colon + 1 ..], " \t");
        for (value) |byte| if ((byte < 0x20 and byte != '\t') or byte == 0x7f) return error.InvalidRequest;
        if (std.ascii.eqlIgnoreCase(name, "authorization")) {
            authorization = value;
            authorization_count += 1;
        } else if (std.ascii.eqlIgnoreCase(name, "content-type")) {
            content_types += 1;
            if (content_types > 1) return error.InvalidRequest;
        } else if (std.ascii.eqlIgnoreCase(name, "content-length")) {
            if (value.len == 0) return error.InvalidRequest;
            for (value) |byte| if (!std.ascii.isDigit(byte)) return error.InvalidRequest;
        } else if (std.ascii.eqlIgnoreCase(name, "transfer-encoding")) {
            transfers += 1;
            if (transfers > 1 or !std.ascii.eqlIgnoreCase(value, "chunked")) return error.InvalidRequest;
        }
    }
    if (transfers != 0 and request.head.content_length != null) return error.InvalidRequest;
    if (authorization_digest) |expected| {
        const actual = authorization orelse return error.Unauthorized;
        if (authorization_count != 1) return error.Unauthorized;
        var digest: [32]u8 = undefined;
        std.crypto.hash.sha2.Sha256.hash(actual, &digest, .{});
        if (!std.crypto.timing_safe.eql([32]u8, digest, expected)) return error.Unauthorized;
    }
    if (request.head.content_length) |length| if (length > max_body_bytes) return error.BodyTooLarge;
    if (request.head.transfer_compression != .identity) return error.UnsupportedMediaType;
    if (request.head.expect) |expect| {
        if (!std.ascii.eqlIgnoreCase(expect, "100-continue")) return error.InvalidRequest;
        request.head.expect = "100-continue";
    }
}

pub fn readObject(request: *std.http.Server.Request, allocator: std.mem.Allocator, optional: bool) !?std.json.Parsed(std.json.Value) {
    const valid_media = if (request.head.content_type) |content_type| mediaType(content_type) else false;
    var buffer: [8192]u8 = undefined;
    const body_reader = try request.readerExpectContinue(&buffer);
    // The reader must observe EOF, including when the body fills the limit.
    // One extra byte distinguishes an exact-size body from oversized input.
    const body = body_reader.allocRemaining(allocator, .limited(max_body_bytes + 1)) catch |err| switch (err) {
        error.StreamTooLong => return error.BodyTooLarge,
        error.OutOfMemory => return err,
        else => return error.InvalidRequest,
    };
    defer allocator.free(body);
    if (body.len > max_body_bytes) return error.BodyTooLarge;
    if (body.len == 0 and optional) return null;
    if (!valid_media) return error.UnsupportedMediaType;
    if (!std.unicode.utf8ValidateSlice(body)) return error.InvalidUtf8;
    const parsed = std.json.parseFromSlice(std.json.Value, allocator, body, .{ .allocate = .alloc_always }) catch |err| {
        if (err == error.OutOfMemory) return err;
        return error.InvalidRequest;
    };
    errdefer parsed.deinit();
    if (parsed.value != .object) return error.InvalidRequest;
    return parsed;
}

fn mediaType(value: []const u8) bool {
    var parts = std.mem.splitScalar(u8, value, ';');
    if (!std.ascii.eqlIgnoreCase(std.mem.trim(u8, parts.first(), " \t"), "application/json")) return false;
    var charset = false;
    while (parts.next()) |part| {
        const equal = std.mem.indexOfScalar(u8, part, '=') orelse return false;
        const name = std.mem.trim(u8, part[0..equal], " \t");
        const parameter = std.mem.trim(u8, part[equal + 1 ..], " \t\"");
        if (std.ascii.eqlIgnoreCase(name, "charset")) {
            if (charset or !std.ascii.eqlIgnoreCase(parameter, "utf-8")) return false;
            charset = true;
        }
    }
    return true;
}
