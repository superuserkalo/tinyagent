const std = @import("std");
const sdk = @import("tinyagent");
const testing = std.testing;
const allocator = testing.allocator;
const io = testing.io;

const Recorder = struct {
    lines: std.ArrayList([]const u8) = .empty,
    fail_at: ?u64 = null,

    fn deinit(self: *Recorder) void {
        for (self.lines.items) |line| allocator.free(line);
        self.lines.deinit(allocator);
    }

    fn handler(self: *Recorder) sdk.EventHandler {
        return .{ .context = self, .callback = record };
    }

    fn record(context: ?*anyopaque, _: std.Io, event: sdk.Event) !void {
        const self: *Recorder = @ptrCast(@alignCast(context.?));
        const line = try std.json.Stringify.valueAlloc(allocator, event, .{});
        self.lines.append(allocator, line) catch |err| {
            allocator.free(line);
            return err;
        };
        if (self.fail_at == event.sequence) return error.ConsumerStopped;
    }

    fn expect(self: *Recorder, expected_types: []const []const u8, status: ?[]const u8) !void {
        try testing.expectEqual(expected_types.len, self.lines.items.len);
        var id: ?[]const u8 = null;
        defer if (id) |value| allocator.free(value);
        for (self.lines.items, expected_types, 1..) |line, expected_type, sequence| {
            const parsed = try std.json.parseFromSlice(std.json.Value, allocator, line, .{});
            defer parsed.deinit();
            const object = parsed.value.object;
            try testing.expectEqualStrings(expected_type, object.get("type").?.string);
            try testing.expectEqual(@as(i64, @intCast(sequence)), object.get("sequence").?.integer);
            const run_id = object.get("runId").?.string;
            if (id == null) id = try allocator.dupe(u8, run_id);
            try testing.expectEqualStrings(id.?, run_id);
            if (sequence == expected_types.len) {
                if (status) |value| try testing.expectEqualStrings(value, object.get("status").?.string);
            }
        }
    }
};

const ProviderFixture = struct {
    mode: enum { text, fail_before, fail_after, swallow, swallow_invalid, tool, invalid_utf8 } = .text,
    calls: usize = 0,
    cleaned: bool = false,
    history_count: usize = 0,

    fn agent(self: *ProviderFixture) sdk.Agent {
        return .{ .provider = .{ .context = self, .stream = stream }, .model = "test", .instructions = "Be brief." };
    }

    fn stream(context: ?*anyopaque, memory: std.mem.Allocator, task_io: std.Io, request: sdk.ProviderRequest, on_text: sdk.TextHandler) !sdk.ProviderTurn {
        const self: *ProviderFixture = @ptrCast(@alignCast(context.?));
        self.calls += 1;
        self.history_count = request.history.len;
        defer self.cleaned = true;
        try testing.expectEqualStrings("test", request.model);
        try testing.expectEqualStrings("Be brief.", request.instructions);
        try testing.expectEqual(0, request.tools.len);
        if (self.mode == .fail_before) return error.ProviderUnavailable;
        if (self.mode == .swallow) {
            on_text.emit(task_io, "first") catch {};
            on_text.emit(task_io, "second") catch {};
            return error.ProviderUnavailable;
        }
        if (self.mode == .invalid_utf8) try on_text.emit(task_io, "\xff");
        if (self.mode == .swallow_invalid) {
            on_text.emit(task_io, "\xff") catch {};
            on_text.emit(task_io, "ignored after failure") catch {};
            return .{ .assistant = .{ .text = "must not complete" } };
        }
        try on_text.emit(task_io, "");
        try on_text.emit(task_io, "Hello ");
        if (self.mode == .fail_after) return error.ProviderUnavailable;
        try on_text.emit(task_io, "Zig 🌍");
        if (self.mode == .tool) {
            const calls = try memory.alloc(sdk.ToolCall, 1);
            calls[0] = .{ .call_id = "c_1", .name = "missing", .arguments = .{ .object = .empty } };
            return .{ .assistant = .{ .text = "", .tool_calls = calls } };
        }
        return .{ .assistant = .{ .text = "Final answer" } };
    }
};

test "ordered UTF-8 deltas, final answer, replayable history, and unique run IDs" {
    var provider: ProviderFixture = .{};
    var recorder: Recorder = .{};
    defer recorder.deinit();
    var first = try provider.agent().run(allocator, io, "Hello", .{ .on_event = recorder.handler() });
    defer first.deinit();
    try recorder.expect(&.{ "agent_start", "text_delta", "text_delta", "agent_end" }, "completed");
    try testing.expectEqualStrings("Final answer", first.text);
    try testing.expectEqualStrings("Hello", first.history[0].user);
    try testing.expectEqualStrings("Final answer", first.history[1].assistant.text);
    try testing.expect(provider.cleaned);
    try testing.expect(std.mem.startsWith(u8, first.run_id, "r_"));
    try testing.expectEqual(34, first.run_id.len);
    var second = try provider.agent().run(allocator, io, "Next", .{ .history = first.history });
    defer second.deinit();
    try testing.expectEqual(3, provider.history_count);
    try testing.expectEqual(4, second.history.len);
    try testing.expectEqual(2, first.history.len);
    try testing.expect(!std.mem.eql(u8, first.run_id, second.run_id));
}

test "provider failures before and during streaming end exactly once" {
    for ([_]@FieldType(ProviderFixture, "mode"){ .fail_before, .fail_after }) |mode| {
        var provider: ProviderFixture = .{ .mode = mode };
        var recorder: Recorder = .{};
        defer recorder.deinit();
        try testing.expectError(error.ProviderUnavailable, provider.agent().run(allocator, io, "Hello", .{ .on_event = recorder.handler() }));
        try recorder.expect(if (mode == .fail_before) &.{ "agent_start", "agent_end" } else &.{ "agent_start", "text_delta", "agent_end" }, "failed");
        try testing.expect(provider.cleaned);
        try testing.expect(std.mem.indexOf(u8, recorder.lines.items[recorder.lines.items.len - 1], "provider_error") != null);
    }
}

test "consumer failures stop delivery, retain their error, and release run storage" {
    for ([_]u64{ 1, 2, 4 }) |sequence| {
        var provider: ProviderFixture = .{};
        var recorder: Recorder = .{ .fail_at = sequence };
        defer recorder.deinit();
        try testing.expectError(error.ConsumerStopped, provider.agent().run(allocator, io, "Hello", .{ .on_event = recorder.handler() }));
        try testing.expectEqual(sequence, recorder.lines.items.len);
        try testing.expectEqual(sequence != 1, provider.cleaned);
    }
    var provider: ProviderFixture = .{ .mode = .swallow };
    var recorder: Recorder = .{ .fail_at = 2 };
    defer recorder.deinit();
    try testing.expectError(error.ConsumerStopped, provider.agent().run(allocator, io, "Hello", .{ .on_event = recorder.handler() }));
    try recorder.expect(&.{ "agent_start", "text_delta" }, null);
    try testing.expect(provider.cleaned);
}

test "unregistered tools and invalid provider text fail without a completed result" {
    for ([_]@FieldType(ProviderFixture, "mode"){ .tool, .invalid_utf8, .swallow_invalid }) |mode| {
        var provider: ProviderFixture = .{ .mode = mode };
        var recorder: Recorder = .{};
        defer recorder.deinit();
        try testing.expectError(if (mode == .tool) error.UnregisteredTool else error.InvalidUtf8, provider.agent().run(allocator, io, "Hello", .{ .on_event = recorder.handler() }));
        try testing.expect(provider.cleaned);
        try recorder.expect(if (mode == .tool) &.{ "agent_start", "text_delta", "text_delta", "agent_end" } else &.{ "agent_start", "agent_end" }, "failed");
    }
}

test "invalid configuration and history are rejected before callbacks or provider entry" {
    var provider: ProviderFixture = .{};
    var recorder: Recorder = .{};
    defer recorder.deinit();
    const options: sdk.RunOptions = .{ .on_event = recorder.handler() };
    var agent = provider.agent();
    agent.model = "";
    try testing.expectError(error.EmptyModel, agent.run(allocator, io, "hello", options));
    agent.model = "\xff";
    try testing.expectError(error.InvalidUtf8, agent.run(allocator, io, "hello", options));
    agent = provider.agent();
    try testing.expectError(error.EmptyIdentifier, agent.run(allocator, io, "hello", .{ .run_id = "", .on_event = recorder.handler() }));
    try testing.expectError(error.InvalidUtf8, agent.run(allocator, io, "\xff", options));
    const bad_history = [_]sdk.HistoryMessage{.{ .assistant = .{
        .text = "old",
        .replay = &.{.{ .number_string = "0,\"injected\":true" }},
    } }};
    try testing.expectError(error.InvalidJsonNumber, agent.run(allocator, io, "hello", .{ .history = &bad_history, .on_event = recorder.handler() }));
    try testing.expectEqual(0, provider.calls);
    try testing.expectEqual(0, recorder.lines.items.len);
}

const BorrowedProvider = struct {
    reply: []const u8,
    metadata: std.json.Value,

    fn stream(context: ?*anyopaque, _: std.mem.Allocator, _: std.Io, _: sdk.ProviderRequest, _: sdk.TextHandler) !sdk.ProviderTurn {
        const self: *BorrowedProvider = @ptrCast(@alignCast(context.?));
        return .{ .assistant = .{ .text = self.reply, .replay = @as(*[1]std.json.Value, &self.metadata) }, .output = @as(*[1]std.json.Value, &self.metadata) };
    }
};

test "completed results own nested metadata, prompts, earlier history, and provider strings" {
    var source = std.heap.ArenaAllocator.init(allocator);
    var source_live = true;
    defer if (source_live) source.deinit();
    const memory = source.allocator();
    const metadata = try std.json.parseFromSlice(std.json.Value, memory, "{\"signature\":{\"parts\":[\"opaque\",123456789012345678901234567890]}}", .{ .parse_numbers = false });
    var provider: BorrowedProvider = .{ .reply = try memory.dupe(u8, "owned answer"), .metadata = metadata.value };
    const agent: sdk.Agent = .{ .provider = .{ .context = &provider, .stream = BorrowedProvider.stream }, .model = "offline" };
    const prior = [_]sdk.HistoryMessage{
        .{ .user = try memory.dupe(u8, "old prompt") },
        .{ .assistant = .{ .text = "thinking", .tool_calls = &.{.{ .call_id = "c_1", .name = "echo", .arguments = metadata.value }}, .replay = &.{metadata.value} } },
        .{ .tool_result = .{ .call_id = "c_1", .name = "echo", .output = "done", .is_error = true } },
    };
    var result = try agent.run(allocator, io, try memory.dupe(u8, "new prompt"), .{ .history = &prior, .run_id = "r_owned" });
    defer result.deinit();
    source.deinit();
    source_live = false;
    try testing.expectEqualStrings("owned answer", result.text);
    try testing.expectEqualStrings("old prompt", result.history[0].user);
    try testing.expectEqualStrings("new prompt", result.history[3].user);
    try testing.expect(result.history[2].tool_result.is_error);
    const signature = result.output[0].object.get("signature").?.object.get("parts").?.array.items;
    try testing.expectEqualStrings("opaque", signature[0].string);
    try testing.expectEqualStrings("123456789012345678901234567890", signature[1].number_string);
    try testing.expectEqualStrings("opaque", result.history[1].assistant.tool_calls[0].arguments.object.get("signature").?.object.get("parts").?.array.items[0].string);
}

const WaitingProvider = struct {
    entered: std.Io.Event = .unset,
    cleaned: bool = false,

    fn stream(context: ?*anyopaque, _: std.mem.Allocator, task_io: std.Io, _: sdk.ProviderRequest, on_text: sdk.TextHandler) !sdk.ProviderTurn {
        const self: *WaitingProvider = @ptrCast(@alignCast(context.?));
        defer self.cleaned = true;
        try on_text.emit(task_io, "partial");
        self.entered.set(task_io);
        var wait: std.Io.Event = .unset;
        try wait.wait(task_io);
        return error.UnexpectedWakeup;
    }
};

test "native task cancellation joins provider cleanup and emits aborted" {
    var provider: WaitingProvider = .{};
    var recorder: Recorder = .{};
    defer recorder.deinit();
    const agent: sdk.Agent = .{ .provider = .{ .context = &provider, .stream = WaitingProvider.stream }, .model = "offline" };
    var future = try io.concurrent(sdk.Agent.run, .{ agent, allocator, io, "hello", sdk.RunOptions{ .on_event = recorder.handler() } });
    try provider.entered.wait(io);
    try testing.expectError(error.Canceled, future.cancel(io));
    try testing.expect(provider.cleaned);
    try recorder.expect(&.{ "agent_start", "text_delta", "agent_end" }, "aborted");
    var next_provider: ProviderFixture = .{};
    var next = try next_provider.agent().run(allocator, io, "still usable", .{});
    defer next.deinit();
}

test "failure to deliver the aborted event returns the consumer error" {
    var provider: WaitingProvider = .{};
    var recorder: Recorder = .{ .fail_at = 3 };
    defer recorder.deinit();
    const agent: sdk.Agent = .{ .provider = .{ .context = &provider, .stream = WaitingProvider.stream }, .model = "offline" };
    var future = try io.concurrent(sdk.Agent.run, .{ agent, allocator, io, "hello", sdk.RunOptions{ .on_event = recorder.handler() } });
    try provider.entered.wait(io);
    try testing.expectError(error.ConsumerStopped, future.cancel(io));
    try recorder.expect(&.{ "agent_start", "text_delta", "agent_end" }, "aborted");
    try testing.expect(provider.cleaned);
}

fn allocationRun(memory: std.mem.Allocator, signal: ?*std.Io.Event) !void {
    var provider: ProviderFixture = .{};
    var result = try provider.agent().run(memory, io, "hello", .{ .run_id = "r_allocations", .cancellation = signal });
    defer result.deinit();
}

test "every allocation failure releases partially constructed run storage" {
    try testing.checkAllAllocationFailures(allocator, allocationRun, .{@as(?*std.Io.Event, null)});
    var signal: std.Io.Event = .unset;
    try testing.checkAllAllocationFailures(allocator, allocationRun, .{@as(?*std.Io.Event, &signal)});
    try testing.expect(!signal.isSet());
}

const HeldCleanup = struct {
    entered: std.Io.Event = .unset,
    cleanup_entered: std.Io.Event = .unset,
    release: std.Io.Event = .unset,
    cancel_finished: std.atomic.Value(bool) = .init(false),

    fn stream(context: ?*anyopaque, _: std.mem.Allocator, task_io: std.Io, _: sdk.ProviderRequest, _: sdk.TextHandler) !sdk.ProviderTurn {
        const self: *HeldCleanup = @ptrCast(@alignCast(context.?));
        defer {
            self.cleanup_entered.set(task_io);
            self.release.waitUncancelable(task_io);
        }
        self.entered.set(task_io);
        var wait: std.Io.Event = .unset;
        try wait.wait(task_io);
        return error.UnexpectedWakeup;
    }

    fn cancel(self: *HeldCleanup, future: *std.Io.Future(anyerror!sdk.RunResult)) !void {
        defer self.cancel_finished.store(true, .release);
        var result = try future.cancel(io);
        result.deinit();
        return error.ExpectedCancellation;
    }
};

test "cancellation remains pending until cooperative provider cleanup finishes" {
    var provider: HeldCleanup = .{};
    var recorder: Recorder = .{};
    defer recorder.deinit();
    const agent: sdk.Agent = .{ .provider = .{ .context = &provider, .stream = HeldCleanup.stream }, .model = "offline" };
    var future = try io.concurrent(sdk.Agent.run, .{ agent, allocator, io, "hello", sdk.RunOptions{ .on_event = recorder.handler() } });
    try provider.entered.wait(io);
    var cancel = io.concurrent(HeldCleanup.cancel, .{ &provider, &future }) catch |err| {
        provider.release.set(io);
        if (future.cancel(io)) |value| {
            var result = value;
            result.deinit();
        } else |_| {}
        return err;
    };
    defer {
        provider.release.set(io);
        cancel.cancel(io) catch {};
    }
    try provider.cleanup_entered.wait(io);
    try testing.expect(!provider.cancel_finished.load(.acquire));
    try testing.expectEqual(1, recorder.lines.items.len);
    provider.release.set(io);
    try testing.expectError(error.Canceled, cancel.await(io));
    try testing.expect(provider.cancel_finished.load(.acquire));
    try recorder.expect(&.{ "agent_start", "agent_end" }, "aborted");
}

const TerminalHandler = struct {
    entered: std.Io.Event = .unset,
    cleaned: bool = false,
    terminal_count: usize = 0,

    fn record(context: ?*anyopaque, task_io: std.Io, event: sdk.Event) !void {
        const self: *TerminalHandler = @ptrCast(@alignCast(context.?));
        if (event.kind != .completed) return;
        self.terminal_count += 1;
        defer self.cleaned = true;
        self.entered.set(task_io);
        var wait: std.Io.Event = .unset;
        try wait.wait(task_io);
    }
};

test "cancelled terminal delivery cannot return completed history or emit another terminal" {
    var provider: ProviderFixture = .{};
    var handler: TerminalHandler = .{};
    var future = try io.concurrent(sdk.Agent.run, .{ provider.agent(), allocator, io, "hello", sdk.RunOptions{
        .history = &.{.{ .user = "committed" }},
        .on_event = .{ .context = &handler, .callback = TerminalHandler.record },
    } });
    try handler.entered.wait(io);
    try testing.expectError(error.Canceled, future.cancel(io));
    try testing.expect(provider.cleaned);
    try testing.expect(handler.cleaned);
    try testing.expectEqual(1, handler.terminal_count);
}

const ConcurrentProvider = struct {
    first_entered: std.Io.Event = .unset,
    second_entered: std.Io.Event = .unset,
    release_second: std.Io.Event = .unset,

    fn stream(context: ?*anyopaque, memory: std.mem.Allocator, task_io: std.Io, request: sdk.ProviderRequest, on_text: sdk.TextHandler) !sdk.ProviderTurn {
        const self: *ConcurrentProvider = @ptrCast(@alignCast(context.?));
        const prompt = request.history[request.history.len - 1].user;
        try on_text.emit(task_io, prompt);
        if (std.mem.eql(u8, prompt, "first")) {
            self.first_entered.set(task_io);
            var wait: std.Io.Event = .unset;
            try wait.wait(task_io);
            return error.UnexpectedWakeup;
        }
        self.second_entered.set(task_io);
        try self.release_second.wait(task_io);
        return .{ .assistant = .{ .text = try memory.dupe(u8, prompt) } };
    }
};

test "one configured agent supports independent concurrent histories and cancellation" {
    var provider: ConcurrentProvider = .{};
    var first_events: Recorder = .{};
    defer first_events.deinit();
    var second_events: Recorder = .{};
    defer second_events.deinit();
    const agent: sdk.Agent = .{ .provider = .{ .context = &provider, .stream = ConcurrentProvider.stream }, .model = "offline" };
    var first = try io.concurrent(sdk.Agent.run, .{ agent, allocator, io, "first", sdk.RunOptions{ .on_event = first_events.handler() } });
    defer if (first.cancel(io)) |value| {
        var result = value;
        result.deinit();
    } else |_| {};
    var second = try io.concurrent(sdk.Agent.run, .{ agent, allocator, io, "second", sdk.RunOptions{
        .history = &.{.{ .user = "only second sees this" }},
        .on_event = second_events.handler(),
    } });
    // This future owns a result even if a test assertion fails before await.
    var second_result: ?sdk.RunResult = null;
    defer {
        if (second_result) |*result| result.deinit() else if (second.cancel(io)) |value| {
            var result = value;
            result.deinit();
        } else |_| {}
    }
    try provider.first_entered.wait(io);
    try provider.second_entered.wait(io);
    try testing.expectError(error.Canceled, first.cancel(io));
    provider.release_second.set(io);
    second_result = try second.await(io);
    try testing.expectEqualStrings("second", second_result.?.text);
    try testing.expectEqual(3, second_result.?.history.len);
    try testing.expectEqualStrings("only second sees this", second_result.?.history[0].user);
    try first_events.expect(&.{ "agent_start", "text_delta", "agent_end" }, "aborted");
    try second_events.expect(&.{ "agent_start", "text_delta", "agent_end" }, "completed");
}

test "an explicit cancellation event interrupts blocked native work and joins cleanup" {
    var signal: std.Io.Event = .unset;
    var provider: WaitingProvider = .{};
    var recorder: Recorder = .{};
    defer recorder.deinit();
    const agent: sdk.Agent = .{ .provider = .{ .context = &provider, .stream = WaitingProvider.stream }, .model = "offline" };
    var future = try io.concurrent(sdk.Agent.run, .{ agent, allocator, io, "hello", sdk.RunOptions{
        .cancellation = &signal,
        .on_event = recorder.handler(),
    } });
    defer if (future.cancel(io)) |value| {
        var result = value;
        result.deinit();
    } else |_| {};
    try provider.entered.wait(io);
    signal.set(io);
    try testing.expectError(error.Canceled, future.await(io));
    try testing.expect(provider.cleaned);
    try recorder.expect(&.{ "agent_start", "text_delta", "agent_end" }, "aborted");
}

test "a previously set cancellation event prevents provider entry" {
    var signal: std.Io.Event = .unset;
    signal.set(io);
    var provider: ProviderFixture = .{};
    var recorder: Recorder = .{};
    defer recorder.deinit();
    try testing.expectError(error.Canceled, provider.agent().run(allocator, io, "hello", .{
        .cancellation = &signal,
        .on_event = recorder.handler(),
    }));
    try testing.expectEqual(0, provider.calls);
    try recorder.expect(&.{ "agent_start", "agent_end" }, "aborted");
    try testing.expect(signal.isSet());
}

const SignalHandler = struct {
    signal: *std.Io.Event,
    recorder: *Recorder,
    cancel_at: u64,

    fn emit(context: ?*anyopaque, task_io: std.Io, event: sdk.Event) !void {
        const self: *SignalHandler = @ptrCast(@alignCast(context.?));
        if (event.sequence == self.cancel_at) self.signal.set(task_io);
        try self.recorder.handler().emit(task_io, event);
    }
};

test "callback cancellation is observed before the next provider or terminal step" {
    for ([_]u64{ 1, 2, 4 }) |sequence| {
        var signal: std.Io.Event = .unset;
        var provider: ProviderFixture = .{};
        var recorder: Recorder = .{};
        defer recorder.deinit();
        var handler: SignalHandler = .{ .signal = &signal, .recorder = &recorder, .cancel_at = sequence };
        try testing.expectError(error.Canceled, provider.agent().run(allocator, io, "hello", .{
            .cancellation = &signal,
            .on_event = .{ .context = &handler, .callback = SignalHandler.emit },
        }));
        try recorder.expect(switch (sequence) {
            1 => &.{ "agent_start", "agent_end" },
            2 => &.{ "agent_start", "text_delta", "agent_end" },
            4 => &.{ "agent_start", "text_delta", "text_delta", "agent_end" },
            else => unreachable,
        }, if (sequence == 4) "completed" else "aborted");
        if (sequence == 1) try testing.expectEqual(0, provider.calls);
    }
}

test "callback failure retains its error when it also requests cancellation" {
    var signal: std.Io.Event = .unset;
    var provider: ProviderFixture = .{ .mode = .swallow };
    var recorder: Recorder = .{ .fail_at = 2 };
    defer recorder.deinit();
    var handler: SignalHandler = .{ .signal = &signal, .recorder = &recorder, .cancel_at = 2 };
    try testing.expectError(error.ConsumerStopped, provider.agent().run(allocator, io, "hello", .{
        .cancellation = &signal,
        .on_event = .{ .context = &handler, .callback = SignalHandler.emit },
    }));
    try recorder.expect(&.{ "agent_start", "text_delta" }, null);
}

test "cancelling a future does not set its caller-owned cancellation event" {
    var signal: std.Io.Event = .unset;
    var provider: WaitingProvider = .{};
    const agent: sdk.Agent = .{ .provider = .{ .context = &provider, .stream = WaitingProvider.stream }, .model = "offline" };
    var future = try io.concurrent(sdk.Agent.run, .{ agent, allocator, io, "hello", sdk.RunOptions{ .cancellation = &signal } });
    defer if (future.cancel(io)) |value| {
        var result = value;
        result.deinit();
    } else |_| {};
    try provider.entered.wait(io);
    try testing.expectError(error.Canceled, future.cancel(io));
    try testing.expect(provider.cleaned);
    try testing.expect(!signal.isSet());
}
