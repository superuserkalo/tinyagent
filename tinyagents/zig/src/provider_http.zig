const std = @import("std");
const json = @import("provider_json.zig");
const history = @import("history.zig");
const types = @import("types.zig");
const Io = std.Io;
const Allocator = std.mem.Allocator;

pub const Options = struct {
    endpoint: []const u8,
    headers: []const std.http.Header,
    timeout_ms: u32,
};

/// One native HTTP request and one timer. Both finish before their borrowed
/// parser, request, and callback state can leave scope. Returned data uses the
/// caller's arena; temporary parsing buffers are reclaimed with this request.
pub fn stream(
    comptime Parser: type,
    allocator: Allocator,
    io: Io,
    options: Options,
    payload: []u8,
    on_text: types.TextHandler,
) !types.ProviderTurn {
    if (options.timeout_ms == 0) return error.InvalidTimeout;
    const uri = try std.Uri.parse(options.endpoint);
    if (uri.user != null or uri.password != null or uri.fragment != null) return error.InvalidEndpoint;
    if (!json.equal(uri.scheme, "http") and !json.equal(uri.scheme, "https")) return error.InvalidEndpoint;
    for (options.headers) |header| {
        if (header.value.len == 0) return error.MissingApiKey;
        for (header.value) |byte| if (byte < 32 or byte > 126) return error.InvalidHeader;
    }
    var arena = std.heap.ArenaAllocator.init(allocator);
    defer arena.deinit();
    const memory = arena.allocator();
    var parser: Parser = .{ .allocator = memory, .on_text = on_text };
    const Outcome = union(enum) { request: anyerror!void, timeout: Io.Cancelable!void };
    var outcomes: [2]Outcome = undefined;
    var select: Io.Select(Outcome) = .init(io, &outcomes);
    defer select.cancelDiscard();
    const Task = struct {
        fn run(memory_allocator: Allocator, task_io: Io, target: std.Uri, headers: []const std.http.Header, body: []u8, response: *Parser) anyerror!void {
            try request(Parser, memory_allocator, task_io, target, headers, body, response);
        }
    };
    try select.concurrent(.request, Task.run, .{ memory, io, uri, options.headers, payload, &parser });
    try select.concurrent(.timeout, Io.sleep, .{ io, Io.Duration.fromMilliseconds(options.timeout_ms), .awake });
    switch (try select.await()) {
        .request => |result| try result,
        .timeout => |result| {
            try result;
            return error.ProviderTimeout;
        },
    }
    // Join even a completed request before reading the state it produced.
    select.cancelDiscard();
    try io.checkCancel();
    const turn = parser.result orelse return error.IncompleteProviderResponse;
    return .{
        .assistant = try history.assistant(allocator, turn.assistant),
        .output = try history.values(allocator, turn.output),
    };
}

fn request(
    comptime Parser: type,
    allocator: Allocator,
    io: Io,
    uri: std.Uri,
    headers: []const std.http.Header,
    payload: []u8,
    parser: *Parser,
) !void {
    var client: std.http.Client = .{ .allocator = allocator, .io = io };
    defer client.deinit();
    const request_headers = try allocator.alloc(std.http.Header, headers.len + 1);
    request_headers[0] = .{ .name = "Accept", .value = "text/event-stream" };
    @memcpy(request_headers[1..], headers);
    var req = try client.request(.POST, uri, .{
        .keep_alive = false,
        .redirect_behavior = .not_allowed,
        .headers = .{
            .content_type = .{ .override = "application/json" },
            .accept_encoding = .{ .override = "identity" },
        },
        // Zig 0.16 does not emit privileged_headers. Redirects are disabled,
        // so credentials belong only to this explicitly configured endpoint.
        .extra_headers = request_headers,
    });
    defer req.deinit();
    // Close immediately at a provider terminal event, including when the peer
    // keeps HTTP framing open. No draining or reusable connection survives.
    defer if (req.connection) |connection| {
        connection.closing = true;
    };
    try req.sendBodyComplete(payload);
    var response = try req.receiveHead(&.{});
    if (@intFromEnum(response.head.status) < 200 or @intFromEnum(response.head.status) >= 300) return error.ProviderHttpError;
    if (response.head.content_encoding != .identity) return error.UnsupportedContentEncoding;
    const content_type = response.head.content_type orelse return error.InvalidContentType;
    var media_type = std.mem.splitScalar(u8, content_type, ';');
    if (!std.ascii.eqlIgnoreCase(std.mem.trim(u8, media_type.first(), " \t"), "text/event-stream")) return error.InvalidContentType;
    var transfer_buffer: [8192]u8 = undefined;
    const reader = response.reader(&transfer_buffer);
    var event_arena = std.heap.ArenaAllocator.init(allocator);
    defer event_arena.deinit();
    var line: std.ArrayList(u8) = .empty;
    defer line.deinit(allocator);
    var data: std.ArrayList(u8) = .empty;
    defer data.deinit(allocator);
    var previous_cr = false;
    var first_line = true;
    while (true) {
        try io.checkCancel();
        const byte = reader.takeByte() catch |err| switch (err) {
            error.EndOfStream => break,
            else => return err,
        };
        if (byte == '\n' and previous_cr) {
            previous_cr = false;
            continue;
        }
        previous_cr = byte == '\r';
        if (byte != '\r' and byte != '\n') {
            try json.append(allocator, &line, &.{byte});
            continue;
        }
        var text: []const u8 = line.items;
        if (first_line and std.mem.startsWith(u8, text, "\xef\xbb\xbf")) text = text[3..];
        first_line = false;
        if (try consumeLine(Parser, allocator, io, &event_arena, text, &data, parser)) return;
        line.clearRetainingCapacity();
    }
    var trailing: []const u8 = line.items;
    if (first_line and std.mem.startsWith(u8, trailing, "\xef\xbb\xbf")) trailing = trailing[3..];
    if (trailing.len != 0) _ = try consumeLine(Parser, allocator, io, &event_arena, trailing, &data, parser);
    if (try consumeLine(Parser, allocator, io, &event_arena, "", &data, parser)) return;
    return error.IncompleteProviderResponse;
}

fn consumeLine(
    comptime Parser: type,
    allocator: Allocator,
    io: Io,
    event_arena: *std.heap.ArenaAllocator,
    line: []const u8,
    data: *std.ArrayList(u8),
    parser: *Parser,
) !bool {
    if (!std.unicode.utf8ValidateSlice(line)) return error.InvalidUtf8;
    if (line.len == 0) {
        if (data.items.len == 0) return false;
        defer data.clearRetainingCapacity();
        const raw = data.items[0 .. data.items.len - 1];
        if (json.equal(raw, "[DONE]")) return false;
        _ = event_arena.reset(.retain_capacity);
        const event = try json.parse(event_arena.allocator(), raw);
        if (event != .object) return error.InvalidProviderResponse;
        try parser.consume(io, event);
        return parser.result != null;
    }
    if (std.mem.startsWith(u8, line, "data:")) {
        var value = line[5..];
        if (std.mem.startsWith(u8, value, " ")) value = value[1..];
        try json.append(allocator, data, value);
        try json.append(allocator, data, "\n");
    } else if (json.equal(line, "data")) {
        try json.append(allocator, data, "\n");
    }
    return false;
}
