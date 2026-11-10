const std = @import("std");
const sdk = @import("tinyagent");
const testing = std.testing;
const io = testing.io;
const allocator = testing.allocator;

const Fixture = struct {
    listener: std.Io.net.Server,
    body: []const u8,
    request_body: ?[]u8 = null,
    request_head: ?[]u8 = null,
    closed: bool = false,
    hold_open: bool = false,
    fragment: bool = false,
    response_head: []const u8 = "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n",
    hold_headers: bool = false,
    entered: std.Io.Event = .unset,

    fn init(body: []const u8) !Fixture {
        const address = try std.Io.net.IpAddress.parse("127.0.0.1", 0);
        return .{ .listener = try address.listen(io, .{ .reuse_address = true }), .body = body };
    }

    fn endpoint(self: *Fixture, memory: std.mem.Allocator) ![]u8 {
        return std.fmt.allocPrint(memory, "http://127.0.0.1:{d}/fixture", .{self.listener.socket.address.getPort()});
    }

    fn deinit(self: *Fixture) void {
        self.listener.deinit(io);
        if (self.request_body) |body| allocator.free(body);
        if (self.request_head) |head| allocator.free(head);
    }

    fn serve(self: *Fixture) !void {
        const connection = try self.listener.accept(io);
        defer connection.close(io);
        defer self.closed = true;
        var read_buffer: [8192]u8 = undefined;
        var reader = connection.reader(io, &read_buffer);
        var head: std.ArrayList(u8) = .empty;
        defer head.deinit(allocator);
        while (!std.mem.endsWith(u8, head.items, "\r\n\r\n")) {
            if (head.items.len == 8192) return error.HeadersTooLarge;
            try head.append(allocator, try reader.interface.takeByte());
        }
        self.request_head = try allocator.dupe(u8, head.items);
        var length: ?usize = null;
        var lines = std.mem.splitSequence(u8, head.items, "\r\n");
        while (lines.next()) |line| {
            const colon = std.mem.findScalar(u8, line, ':') orelse continue;
            if (std.ascii.eqlIgnoreCase(line[0..colon], "content-length")) {
                length = try std.fmt.parseInt(usize, std.mem.trim(u8, line[colon + 1 ..], " "), 10);
            }
        }
        self.request_body = try reader.interface.readAlloc(allocator, length orelse return error.MissingLength);
        var writer = connection.writer(io, &.{});
        self.respond(&writer.interface) catch |err| {
            if (writer.err) |cause| {
                if (cause == error.ConnectionResetByPeer or cause == error.SocketUnconnected) return;
            }
            return err;
        };
        // EOF proves that a provider terminal event or cancellation closed its
        // native request, including when the HTTP terminator was withheld.
        _ = reader.interface.takeByte() catch |err| switch (err) {
            error.EndOfStream => return,
            error.ReadFailed => if (reader.err.? == error.ConnectionResetByPeer) return else return err,
        };
        return error.UnexpectedRequestBytes;
    }

    fn respond(self: *Fixture, writer: *std.Io.Writer) !void {
        if (!self.hold_headers) try writer.writeAll(self.response_head);
        self.entered.set(io);
        if (self.hold_headers) {
            // Wait below for the client to close its pending header read.
        } else if (self.fragment) {
            for (self.body) |byte| {
                try writer.writeAll("1\r\n");
                try writer.writeByte(byte);
                try writer.writeAll("\r\n");
            }
        } else if (self.body.len != 0) {
            try writer.print("{x}\r\n{s}\r\n", .{ self.body.len, self.body });
        }
        if (!self.hold_open and !self.hold_headers) try writer.writeAll("0\r\n\r\n");
    }
};

const Text = struct {
    bytes: std.ArrayList(u8) = .empty,
    fail: bool = false,

    fn deinit(self: *Text) void {
        self.bytes.deinit(allocator);
    }

    fn handler(self: *Text) sdk.TextHandler {
        return .{ .context = self, .callback = append };
    }

    fn append(context: ?*anyopaque, _: std.Io, value: []const u8) !void {
        const self: *Text = @ptrCast(@alignCast(context.?));
        if (self.fail) return error.ConsumerStopped;
        try self.bytes.appendSlice(allocator, value);
    }
};

fn runProvider(provider: sdk.Provider, memory: std.mem.Allocator, request: sdk.ProviderRequest, handler: sdk.TextHandler) anyerror!sdk.ProviderTurn {
    return provider.stream(provider.context, memory, io, request, handler);
}

fn failureCase(anthropic: bool, body: []const u8, expected: anyerror) !void {
    var arena = std.heap.ArenaAllocator.init(allocator);
    defer arena.deinit();
    var fixture = try Fixture.init(body);
    defer fixture.deinit();
    var server = try io.concurrent(Fixture.serve, .{&fixture});
    defer server.cancel(io) catch {};
    const endpoint = try fixture.endpoint(arena.allocator());
    var openai: sdk.OpenAIProvider = .{ .api_key = "test-key", .endpoint = endpoint };
    var claude: sdk.AnthropicProvider = .{ .api_key = "test-key", .endpoint = endpoint };
    var text: Text = .{};
    defer text.deinit();
    try testing.expectError(expected, runProvider(if (anthropic) claude.provider() else openai.provider(), arena.allocator(), .{ .model = "test", .instructions = "", .history = &.{} }, text.handler()));
    try server.await(io);
    try testing.expect(fixture.closed);
}

test "OpenAI rejects malformed, incomplete, and failed streams" {
    const cases = [_]struct { []const u8, anyerror }{
        .{ "data: {\"type\":\"response.failed\"}\n\n", error.ProviderStreamFailed },
        .{ "data: {\"type\":\"response.incomplete\"}\n\n", error.ProviderStreamFailed },
        .{ "data: {\"type\":\"error\"}\n\n", error.ProviderStreamFailed },
        .{ "data: {\"type\":\"response.output_text.delta\",\"delta\":4}\n\n", error.InvalidProviderResponse },
        .{ "data: {\"type\":\"response.output_text.delta\",\"delta\":\"partial\"}\n\n", error.IncompleteProviderResponse },
        .{ "data: {\"type\":\"response.completed\",\"response\":{\"status\":\"incomplete\",\"output\":[]}}\n\n", error.IncompleteProviderResponse },
        .{ "data: {\"type\":\"response.completed\"}\n\n", error.InvalidProviderResponse },
        .{ "data: {\"type\":\"response.completed\",\"response\":{}}\n\n", error.IncompleteProviderResponse },
        .{ "data: {\"type\":\"response.function_call_arguments.delta\",\"output_index\":-1,\"delta\":\"{}\"}\n\n", error.InvalidProviderResponse },
        .{ "data: {\"type\":\"response.completed\",\"response\":{\"output\":[{\"type\":\"function_call\",\"call_id\":\"c\",\"name\":\"echo\",\"arguments\":\"[]\"}]}}\n\n", error.InvalidToolArguments },
        .{ "data: {\"type\":\"response.completed\",\"response\":{\"output\":[{\"type\":\"function_call\",\"call_id\":\"c\",\"name\":\"echo\",\"arguments\":\"{\"}]}}\n\n", error.InvalidProviderResponse },
        .{ "data: []\n\n", error.InvalidProviderResponse },
        .{ "data: {\"type\":\"unknown\",\"type\":\"duplicate\"}\n\n", error.InvalidProviderResponse },
        .{ "data: \xff\n\n", error.InvalidUtf8 },
        .{ "data: [DONE]\n\n", error.IncompleteProviderResponse },
    };
    for (cases) |case| try failureCase(false, case[0], case[1]);
}

test "Anthropic rejects malformed block ordering, arguments, and unfinished messages" {
    const start = "data: {\"type\":\"message_start\",\"message\":{}}\n\n";
    const stop = "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"end_turn\"}}\n\ndata: {\"type\":\"message_stop\"}\n\n";
    const cases = [_]struct { []const u8, anyerror }{
        .{ "data: {\"type\":\"error\"}\n\n", error.ProviderStreamFailed },
        .{ start, error.IncompleteProviderResponse },
        .{ start ++ start, error.InvalidProviderResponse },
        .{ "data: {\"type\":\"message_stop\"}\n\n", error.IncompleteProviderResponse },
        .{ "data: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n", error.InvalidProviderResponse },
        .{ start ++ "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"hi\"}}\n\n", error.InvalidProviderResponse },
        .{ start ++ "data: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n" ++ stop, error.IncompleteProviderResponse },
        .{ start ++ "data: {\"type\":\"content_block_stop\",\"index\":0}\n\n", error.InvalidProviderResponse },
        .{ start ++ "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"max_tokens\"}}\n\ndata: {\"type\":\"message_stop\"}\n\n", error.IncompleteProviderResponse },
        .{ start ++ "data: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"tool_use\",\"id\":\"c\",\"name\":\"echo\",\"input\":[]}}\n\ndata: {\"type\":\"content_block_stop\",\"index\":0}\n\n", error.InvalidToolArguments },
    };
    for (cases) |case| try failureCase(true, case[0], case[1]);
}

test "both providers serialize replay and registered tool definitions over native HTTP" {
    for ([_]bool{ false, true }) |anthropic| {
        var arena = std.heap.ArenaAllocator.init(allocator);
        defer arena.deinit();
        const memory = arena.allocator();
        var first_fixture = try Fixture.init(if (anthropic) anthropic_tool else openai_tool);
        defer first_fixture.deinit();
        var first_server = try io.concurrent(Fixture.serve, .{&first_fixture});
        defer first_server.cancel(io) catch {};
        var openai: sdk.OpenAIProvider = .{ .api_key = "test-key", .endpoint = try first_fixture.endpoint(memory) };
        var claude: sdk.AnthropicProvider = .{ .api_key = "test-key", .endpoint = openai.endpoint };
        var text: Text = .{};
        defer text.deinit();
        const first = try runProvider(if (anthropic) claude.provider() else openai.provider(), memory, .{ .model = "test", .instructions = "", .history = &.{} }, text.handler());
        try first_server.await(io);
        var fixture = try Fixture.init(if (anthropic) anthropic_text else openai_text);
        defer fixture.deinit();
        var server = try io.concurrent(Fixture.serve, .{&fixture});
        defer server.cancel(io) catch {};
        openai.endpoint = try fixture.endpoint(memory);
        claude.endpoint = openai.endpoint;
        const schema = try std.json.parseFromSlice(std.json.Value, memory, "{\"type\":\"object\",\"properties\":{\"text\":{\"type\":\"string\"}}}", .{});
        _ = try runProvider(if (anthropic) claude.provider() else openai.provider(), memory, .{
            .model = "test",
            .instructions = "",
            .history = &.{ .{ .user = "use echo" }, .{ .assistant = first.assistant }, .{ .tool_result = .{ .call_id = "call_1", .name = "echo", .output = "café", .is_error = true } } },
            .tools = &.{.{ .name = "echo", .description = "Echo text", .parameters = schema.value }},
        }, text.handler());
        try server.await(io);
        const request = try std.json.parseFromSlice(std.json.Value, memory, fixture.request_body.?, .{});
        const object = request.value.object;
        const tool = object.get("tools").?.array.items[0].object;
        try testing.expectEqualStrings("echo", tool.get("name").?.string);
        if (anthropic) {
            try testing.expectEqualStrings("object", tool.get("input_schema").?.object.get("type").?.string);
            const messages = object.get("messages").?.array.items;
            try testing.expectEqualStrings("signed-fixture", messages[1].object.get("content").?.array.items[0].object.get("signature").?.string);
            try testing.expect(messages[2].object.get("content").?.array.items[0].object.get("is_error").?.bool);
        } else {
            try testing.expect(tool.get("strict").?.bool);
            try testing.expectEqualStrings("object", tool.get("parameters").?.object.get("type").?.string);
            const input = object.get("input").?.array.items;
            try testing.expectEqualStrings("opaque-fixture", input[1].object.get("encrypted_content").?.string);
            try testing.expectEqualStrings("{\"text\":\"café\"}", input[2].object.get("arguments").?.string);
            try testing.expectEqualStrings("function_call_output", input[3].object.get("type").?.string);
        }
    }
}

test "consumer failure and cancellation during response headers release both providers" {
    for ([_]bool{ false, true }) |anthropic| {
        for ([_]bool{ false, true }) |headers| {
            var arena = std.heap.ArenaAllocator.init(allocator);
            defer arena.deinit();
            var fixture = try Fixture.init(if (anthropic) anthropic_text else openai_text);
            defer fixture.deinit();
            fixture.hold_headers = headers;
            fixture.hold_open = true;
            var server = try io.concurrent(Fixture.serve, .{&fixture});
            defer server.cancel(io) catch {};
            var openai: sdk.OpenAIProvider = .{ .api_key = "test-key", .endpoint = try fixture.endpoint(arena.allocator()) };
            var claude: sdk.AnthropicProvider = .{ .api_key = "test-key", .endpoint = openai.endpoint };
            var text: Text = .{ .fail = !headers };
            defer text.deinit();
            var running = try io.concurrent(runProvider, .{ if (anthropic) claude.provider() else openai.provider(), arena.allocator(), sdk.ProviderRequest{ .model = "test", .instructions = "", .history = &.{} }, text.handler() });
            defer _ = running.cancel(io) catch {};
            try fixture.entered.wait(io);
            if (headers) {
                try testing.expectError(error.Canceled, running.cancel(io));
            } else {
                try testing.expectError(error.ConsumerStopped, running.await(io));
            }
            try server.await(io);
        }
    }
}

const openai_text = @embedFile("fixtures/openai_text.sse");
const openai_tool = @embedFile("fixtures/openai_tool.sse");
const anthropic_text = @embedFile("fixtures/anthropic_text.sse");
const anthropic_tool = @embedFile("fixtures/anthropic_tool.sse");

test "OpenAI native chunked streaming, UTF-8 fragments, JSON request, and terminal cleanup" {
    var arena = std.heap.ArenaAllocator.init(allocator);
    defer arena.deinit();
    var fixture = try Fixture.init(openai_text);
    defer fixture.deinit();
    fixture.fragment = true;
    fixture.hold_open = true;
    var server = try io.concurrent(Fixture.serve, .{&fixture});
    defer server.cancel(io) catch {};
    var provider: sdk.OpenAIProvider = .{ .api_key = "test-key", .endpoint = try fixture.endpoint(arena.allocator()) };
    var text: Text = .{};
    defer text.deinit();
    const result = try runProvider(provider.provider(), arena.allocator(), .{ .model = "test", .instructions = "be brief", .history = &.{.{ .user = "hello" }} }, text.handler());
    try server.await(io);
    try testing.expect(fixture.closed);
    try testing.expectEqualStrings("done\ncafé", result.assistant.text);
    try testing.expectEqualStrings(result.assistant.text, text.bytes.items);
    try testing.expectEqual(0, result.assistant.tool_calls.len);
    const request = try std.json.parseFromSlice(std.json.Value, allocator, fixture.request_body.?, .{});
    defer request.deinit();
    try testing.expect(request.value.object.get("stream").?.bool);
    try testing.expect(!request.value.object.get("store").?.bool);
    const included = request.value.object.get("include") orelse return error.MissingReasoningRequest;
    try testing.expectEqualStrings("reasoning.encrypted_content", included.array.items[0].string);
    try testing.expect(!request.value.object.get("parallel_tool_calls").?.bool);
    try testing.expectEqualStrings("hello", request.value.object.get("input").?.array.items[0].object.get("content").?.string);
    try testing.expect(std.mem.find(u8, fixture.request_head.?, "Authorization: Bearer test-key") != null);
}

test "OpenAI arguments finish before tool calls and opaque reasoning survives replay" {
    var arena = std.heap.ArenaAllocator.init(allocator);
    defer arena.deinit();
    var fixture = try Fixture.init(openai_tool);
    defer fixture.deinit();
    var server = try io.concurrent(Fixture.serve, .{&fixture});
    defer server.cancel(io) catch {};
    var provider: sdk.OpenAIProvider = .{ .api_key = "test-key", .endpoint = try fixture.endpoint(arena.allocator()) };
    var text: Text = .{};
    defer text.deinit();
    const result = try runProvider(provider.provider(), arena.allocator(), .{ .model = "test", .instructions = "", .history = &.{} }, text.handler());
    try server.await(io);
    try testing.expectEqualStrings("Working ", text.bytes.items);
    try testing.expectEqualStrings("", result.assistant.text);
    try testing.expectEqualStrings("café", result.assistant.tool_calls[0].arguments.object.get("text").?.string);
    try testing.expectEqualStrings("opaque-fixture", result.assistant.replay[0].object.get("encrypted_content").?.string);
    try testing.expectEqualStrings("{\"text\":\"café\"}", result.assistant.replay[1].object.get("arguments").?.string);
}

test "OpenAI cancellation and timeout close a silent native response" {
    for ([_]bool{ false, true }) |timeout| {
        var arena = std.heap.ArenaAllocator.init(allocator);
        defer arena.deinit();
        var fixture = try Fixture.init("");
        defer fixture.deinit();
        fixture.hold_open = true;
        var server = try io.concurrent(Fixture.serve, .{&fixture});
        defer server.cancel(io) catch {};
        var provider: sdk.OpenAIProvider = .{ .api_key = "test-key", .endpoint = try fixture.endpoint(arena.allocator()), .timeout_ms = if (timeout) 1000 else 180_000 };
        var text: Text = .{};
        defer text.deinit();
        var running = try io.concurrent(runProvider, .{ provider.provider(), arena.allocator(), sdk.ProviderRequest{ .model = "test", .instructions = "", .history = &.{} }, text.handler() });
        defer _ = running.cancel(io) catch {};
        try fixture.entered.wait(io);
        if (timeout) {
            try testing.expectError(error.ProviderTimeout, running.await(io));
        } else {
            try testing.expectError(error.Canceled, running.cancel(io));
        }
        try server.await(io);
        try testing.expect(fixture.closed);
    }
}

test "Anthropic native text stream, request headers, message content, and usage" {
    var arena = std.heap.ArenaAllocator.init(allocator);
    defer arena.deinit();
    var fixture = try Fixture.init(anthropic_text);
    defer fixture.deinit();
    fixture.fragment = true;
    fixture.hold_open = true;
    var server = try io.concurrent(Fixture.serve, .{&fixture});
    defer server.cancel(io) catch {};
    var provider: sdk.AnthropicProvider = .{ .api_key = "test-key", .endpoint = try fixture.endpoint(arena.allocator()), .max_tokens = 1024 };
    var text: Text = .{};
    defer text.deinit();
    const result = try runProvider(provider.provider(), arena.allocator(), .{ .model = "test", .instructions = "be brief", .history = &.{.{ .user = "hello" }} }, text.handler());
    try server.await(io);
    try testing.expectEqualStrings("done\ncafé", result.assistant.text);
    try testing.expectEqualStrings(result.assistant.text, text.bytes.items);
    try testing.expectEqualStrings("end_turn", result.assistant.replay[0].object.get("stop_reason").?.string);
    const request = try std.json.parseFromSlice(std.json.Value, allocator, fixture.request_body.?, .{});
    defer request.deinit();
    try testing.expectEqual(1024, request.value.object.get("max_tokens").?.integer);
    try testing.expectEqualStrings("be brief", request.value.object.get("system").?.string);
    try testing.expectEqualStrings("hello", request.value.object.get("messages").?.array.items[0].object.get("content").?.array.items[0].object.get("text").?.string);
    try testing.expect(std.mem.find(u8, fixture.request_head.?, "x-api-key: test-key") != null);
    try testing.expect(std.mem.find(u8, fixture.request_head.?, "anthropic-version: 2023-06-01") != null);
}

test "Anthropic complete tool arguments, thinking signatures, and top-level metadata" {
    var arena = std.heap.ArenaAllocator.init(allocator);
    defer arena.deinit();
    var fixture = try Fixture.init(anthropic_tool);
    defer fixture.deinit();
    var server = try io.concurrent(Fixture.serve, .{&fixture});
    defer server.cancel(io) catch {};
    var provider: sdk.AnthropicProvider = .{ .api_key = "test-key", .endpoint = try fixture.endpoint(arena.allocator()) };
    var text: Text = .{};
    defer text.deinit();
    const result = try runProvider(provider.provider(), arena.allocator(), .{ .model = "test", .instructions = "", .history = &.{} }, text.handler());
    try server.await(io);
    try testing.expectEqualStrings("", text.bytes.items);
    try testing.expectEqualStrings("café", result.assistant.tool_calls[0].arguments.object.get("text").?.string);
    const replay = result.assistant.replay[0].object;
    try testing.expectEqualStrings("keep", replay.get("fixture_metadata").?.string);
    try testing.expectEqualStrings("signed-fixture", replay.get("content").?.array.items[0].object.get("signature").?.string);
    try testing.expectEqualStrings("8", replay.get("usage").?.object.get("input_tokens").?.number_string);
    try testing.expectEqualStrings("12", replay.get("usage").?.object.get("output_tokens").?.number_string);
}

test "Anthropic cancellation and timeout close a silent native response" {
    for ([_]bool{ false, true }) |timeout| {
        var arena = std.heap.ArenaAllocator.init(allocator);
        defer arena.deinit();
        var fixture = try Fixture.init("");
        defer fixture.deinit();
        fixture.hold_open = true;
        var server = try io.concurrent(Fixture.serve, .{&fixture});
        defer server.cancel(io) catch {};
        var provider: sdk.AnthropicProvider = .{ .api_key = "test-key", .endpoint = try fixture.endpoint(arena.allocator()), .timeout_ms = if (timeout) 1000 else 180_000 };
        var text: Text = .{};
        defer text.deinit();
        var running = try io.concurrent(runProvider, .{ provider.provider(), arena.allocator(), sdk.ProviderRequest{ .model = "test", .instructions = "", .history = &.{} }, text.handler() });
        defer _ = running.cancel(io) catch {};
        try fixture.entered.wait(io);
        if (timeout) {
            try testing.expectError(error.ProviderTimeout, running.await(io));
        } else {
            try testing.expectError(error.Canceled, running.cancel(io));
        }
        try server.await(io);
        try testing.expect(fixture.closed);
    }
}

test "SSE line limits and cumulative argument limits apply before completion" {
    var arena = std.heap.ArenaAllocator.init(allocator);
    defer arena.deinit();
    const memory = arena.allocator();
    const large = try memory.alloc(u8, 1024 * 1024 + 1);
    @memset(large, 'x');
    const oversized = try std.fmt.allocPrint(memory, "data: {{\"type\":\"unknown\",\"padding\":\"{s}\"}}\n\n", .{large});
    for ([_]bool{ false, true }) |anthropic| try failureCase(anthropic, oversized, error.ProviderDataTooLarge);
    const first = large[0..600_000];
    const second = large[0..500_000];
    const openai = try std.fmt.allocPrint(memory, "data: {{\"type\":\"response.function_call_arguments.delta\",\"output_index\":0,\"delta\":\"{s}\"}}\n\n" ++
        "data: {{\"type\":\"response.function_call_arguments.delta\",\"output_index\":0,\"delta\":\"{s}\"}}\n\n", .{ first, second });
    try failureCase(false, openai, error.ProviderDataTooLarge);
    const anthropic = try std.fmt.allocPrint(memory, "data: {{\"type\":\"message_start\",\"message\":{{}}}}\n\n" ++
        "data: {{\"type\":\"content_block_start\",\"index\":0,\"content_block\":{{\"type\":\"tool_use\",\"id\":\"c\",\"name\":\"echo\",\"input\":{{}}}}}}\n\n" ++
        "data: {{\"type\":\"content_block_delta\",\"index\":0,\"delta\":{{\"type\":\"input_json_delta\",\"partial_json\":\"{s}\"}}}}\n\n" ++
        "data: {{\"type\":\"content_block_delta\",\"index\":0,\"delta\":{{\"type\":\"input_json_delta\",\"partial_json\":\"{s}\"}}}}\n\n", .{ first, second });
    try failureCase(true, anthropic, error.ProviderDataTooLarge);
}

test "BOM, CRLF, bare CR, unknown events, and unterminated final SSE data" {
    const cases = [_][]const u8{
        "\xef\xbb\xbf: comment\r\nevent: ignored\r\ndata: {\"type\":\"future_event\"}\r\n\r\ndata: {\"type\":\"response.completed\",\"response\":{\"output\":[]}}\r\n\r\n",
        "data: {\"type\":\"response.completed\",\"response\":{\"output\":[]}}\r\r",
        "data: {\"type\":\"response.completed\",\"response\":{\"output\":[]}}",
        "\xef\xbb\xbfdata: {\"type\":\"response.completed\",\"response\":{\"output\":[]}}",
    };
    for (cases) |body| {
        var arena = std.heap.ArenaAllocator.init(allocator);
        defer arena.deinit();
        var fixture = try Fixture.init(body);
        defer fixture.deinit();
        var server = try io.concurrent(Fixture.serve, .{&fixture});
        defer server.cancel(io) catch {};
        var provider: sdk.OpenAIProvider = .{ .api_key = "test-key", .endpoint = try fixture.endpoint(arena.allocator()) };
        var text: Text = .{};
        defer text.deinit();
        const result = try runProvider(provider.provider(), arena.allocator(), .{ .model = "test", .instructions = "", .history = &.{} }, text.handler());
        try server.await(io);
        try testing.expectEqualStrings("", result.assistant.text);
    }
}

test "HTTP errors, wrong content type, compression, and redirects are rejected" {
    const cases = [_]struct { []const u8, anyerror }{
        .{ "HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n", error.ProviderHttpError },
        .{ "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 0\r\n\r\n", error.InvalidContentType },
        .{ "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Encoding: gzip\r\nContent-Length: 0\r\n\r\n", error.UnsupportedContentEncoding },
        .{ "HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/credentials-must-not-follow\r\nContent-Length: 0\r\n\r\n", error.TooManyHttpRedirects },
    };
    for ([_]bool{ false, true }) |anthropic| {
        for (cases) |case| {
            var arena = std.heap.ArenaAllocator.init(allocator);
            defer arena.deinit();
            var fixture = try Fixture.init("");
            defer fixture.deinit();
            fixture.response_head = case[0];
            fixture.hold_open = true;
            var server = try io.concurrent(Fixture.serve, .{&fixture});
            defer server.cancel(io) catch {};
            var openai: sdk.OpenAIProvider = .{ .api_key = "test-key", .endpoint = try fixture.endpoint(arena.allocator()) };
            var claude: sdk.AnthropicProvider = .{ .api_key = "test-key", .endpoint = openai.endpoint };
            var text: Text = .{};
            defer text.deinit();
            try testing.expectError(case[1], runProvider(if (anthropic) claude.provider() else openai.provider(), arena.allocator(), .{ .model = "test", .instructions = "", .history = &.{} }, text.handler()));
            try server.await(io);
        }
    }
}

const SlowConsumer = struct {
    entered: std.Io.Event = .unset,
    cleaned: bool = false,

    fn receive(context: ?*anyopaque, task_io: std.Io, _: []const u8) !void {
        const self: *SlowConsumer = @ptrCast(@alignCast(context.?));
        defer self.cleaned = true;
        self.entered.set(task_io);
        var wait: std.Io.Event = .unset;
        try wait.wait(task_io);
    }
};

test "the deadline cancels a stalled callback even with completion already buffered" {
    for ([_]bool{ false, true }) |anthropic| {
        var arena = std.heap.ArenaAllocator.init(allocator);
        defer arena.deinit();
        var fixture = try Fixture.init(if (anthropic) anthropic_text else openai_text);
        defer fixture.deinit();
        var server = try io.concurrent(Fixture.serve, .{&fixture});
        defer server.cancel(io) catch {};
        var openai: sdk.OpenAIProvider = .{ .api_key = "test-key", .endpoint = try fixture.endpoint(arena.allocator()), .timeout_ms = 1000 };
        var claude: sdk.AnthropicProvider = .{ .api_key = "test-key", .endpoint = openai.endpoint, .timeout_ms = 1000 };
        var consumer: SlowConsumer = .{};
        var running = try io.concurrent(runProvider, .{ if (anthropic) claude.provider() else openai.provider(), arena.allocator(), sdk.ProviderRequest{ .model = "test", .instructions = "", .history = &.{} }, sdk.TextHandler{ .context = &consumer, .callback = SlowConsumer.receive } });
        defer _ = running.cancel(io) catch {};
        try consumer.entered.wait(io);
        try testing.expectError(error.ProviderTimeout, running.await(io));
        try testing.expect(consumer.cleaned);
        try server.await(io);
    }
}

const AgentEvents = struct {
    sequence: u64 = 0,
    completed: bool = false,

    fn receive(context: ?*anyopaque, _: std.Io, event: sdk.Event) !void {
        const self: *AgentEvents = @ptrCast(@alignCast(context.?));
        self.sequence += 1;
        try testing.expectEqual(self.sequence, event.sequence);
        if (self.sequence == 1) try testing.expect(event.kind == .agent_start);
        if (event.kind == .completed) self.completed = true;
    }
};

test "both native providers complete through Agent and return detached replay history" {
    for ([_]bool{ false, true }) |anthropic| {
        var arena = std.heap.ArenaAllocator.init(allocator);
        defer arena.deinit();
        var fixture = try Fixture.init(if (anthropic) anthropic_text else openai_text);
        defer fixture.deinit();
        var server = try io.concurrent(Fixture.serve, .{&fixture});
        defer server.cancel(io) catch {};
        var openai: sdk.OpenAIProvider = .{ .api_key = "test-key", .endpoint = try fixture.endpoint(arena.allocator()) };
        var claude: sdk.AnthropicProvider = .{ .api_key = "test-key", .endpoint = openai.endpoint };
        const agent: sdk.Agent = .{ .model = "test", .provider = if (anthropic) claude.provider() else openai.provider() };
        var events: AgentEvents = .{};
        var result = try agent.run(allocator, io, "hello", .{ .on_event = .{ .context = &events, .callback = AgentEvents.receive } });
        defer result.deinit();
        try server.await(io);
        try testing.expect(events.completed);
        try testing.expectEqual(4, events.sequence);
        try testing.expectEqual(2, result.history.len);
        try testing.expectEqualStrings("done\ncafé", result.text);
        try testing.expect(result.history[1].assistant.replay.len != 0);
    }
}

test "invalid provider options fail before a network request" {
    var arena = std.heap.ArenaAllocator.init(allocator);
    defer arena.deinit();
    var text: Text = .{};
    defer text.deinit();
    const request: sdk.ProviderRequest = .{ .model = "test", .instructions = "", .history = &.{} };
    var openai: sdk.OpenAIProvider = .{ .api_key = "", .endpoint = "http://127.0.0.1:1" };
    var claude: sdk.AnthropicProvider = .{ .api_key = "", .endpoint = openai.endpoint };
    try testing.expectError(error.MissingApiKey, runProvider(openai.provider(), arena.allocator(), request, text.handler()));
    try testing.expectError(error.MissingApiKey, runProvider(claude.provider(), arena.allocator(), request, text.handler()));
    openai.api_key = "bad\r\nheader";
    claude.api_key = openai.api_key;
    try testing.expectError(error.InvalidHeader, runProvider(openai.provider(), arena.allocator(), request, text.handler()));
    try testing.expectError(error.InvalidHeader, runProvider(claude.provider(), arena.allocator(), request, text.handler()));
    openai.api_key = "test-key";
    openai.timeout_ms = 0;
    try testing.expectError(error.InvalidTimeout, runProvider(openai.provider(), arena.allocator(), request, text.handler()));
    openai.timeout_ms = 1000;
    for ([_][]const u8{ "ftp://127.0.0.1/file", "http://user:password@127.0.0.1/", "http://127.0.0.1/#fragment" }) |endpoint| {
        openai.endpoint = endpoint;
        try testing.expectError(error.InvalidEndpoint, runProvider(openai.provider(), arena.allocator(), request, text.handler()));
    }
    claude.api_key = "test-key";
    claude.max_tokens = 0;
    try testing.expectError(error.InvalidMaxTokens, runProvider(claude.provider(), arena.allocator(), request, text.handler()));
}

test "neutral tool history serializes without replay and adjacent Anthropic results stay together" {
    for ([_]bool{ false, true }) |anthropic| {
        var arena = std.heap.ArenaAllocator.init(allocator);
        defer arena.deinit();
        const memory = arena.allocator();
        var fixture = try Fixture.init(if (anthropic) anthropic_text else openai_text);
        defer fixture.deinit();
        var server = try io.concurrent(Fixture.serve, .{&fixture});
        defer server.cancel(io) catch {};
        var openai: sdk.OpenAIProvider = .{ .api_key = "test-key", .endpoint = try fixture.endpoint(memory) };
        var claude: sdk.AnthropicProvider = .{ .api_key = "test-key", .endpoint = openai.endpoint };
        var text: Text = .{};
        defer text.deinit();
        const arguments = try std.json.parseFromSlice(std.json.Value, memory, "{\"text\":\"café\",\"large\":123456789012345678901234567890}", .{ .parse_numbers = false });
        _ = try runProvider(if (anthropic) claude.provider() else openai.provider(), memory, .{
            .model = "test",
            .instructions = "",
            .history = &.{
                .{ .assistant = .{ .text = "working", .tool_calls = &.{
                    .{ .call_id = "c_1", .name = "echo", .arguments = arguments.value },
                    .{ .call_id = "c_2", .name = "echo", .arguments = arguments.value },
                } } },
                .{ .tool_result = .{ .call_id = "c_1", .name = "echo", .output = "first" } },
                .{ .tool_result = .{ .call_id = "c_2", .name = "echo", .output = "second", .is_error = true } },
            },
        }, text.handler());
        try server.await(io);
        const request = try std.json.parseFromSlice(std.json.Value, memory, fixture.request_body.?, .{ .parse_numbers = false });
        if (anthropic) {
            const messages = request.value.object.get("messages").?.array.items;
            try testing.expectEqual(2, messages.len);
            const calls = messages[0].object.get("content").?.array.items;
            try testing.expectEqualStrings("123456789012345678901234567890", calls[1].object.get("input").?.object.get("large").?.number_string);
            const results = messages[1].object.get("content").?.array.items;
            try testing.expectEqual(2, results.len);
            try testing.expectEqualStrings("c_2", results[1].object.get("tool_use_id").?.string);
            try testing.expect(results[1].object.get("is_error").?.bool);
        } else {
            const input = request.value.object.get("input").?.array.items;
            try testing.expectEqual(5, input.len);
            const args = try std.json.parseFromSlice(std.json.Value, memory, input[1].object.get("arguments").?.string, .{ .parse_numbers = false });
            try testing.expectEqualStrings("123456789012345678901234567890", args.value.object.get("large").?.number_string);
            try testing.expectEqualStrings("second", input[4].object.get("output").?.string);
        }
    }
}
