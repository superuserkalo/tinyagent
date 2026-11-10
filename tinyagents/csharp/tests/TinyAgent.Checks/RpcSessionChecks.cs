using System.Collections.Concurrent;
using System.Text.Json.Nodes;

namespace TinyAgent.Checks;

internal static class RpcSessionChecks
{
    internal static IEnumerable<(string Name, Func<Task> Run)> Cases =>
    [
        ("RPC session shared command fixtures", SharedFixtures),
        ("RPC session acknowledgement ordering and admission", Acknowledgement),
        ("RPC session cancellation during pending acknowledgement", CancelAcknowledgement),
        ("RPC session history commit after final delivery", Commit),
        ("RPC session provider, consumer, and cancellation rollback", Rollback),
        ("RPC session abort acknowledges before cooperative cleanup", Abort),
        ("RPC session reset waits and rejects overlapping work", Reset),
        ("RPC session disposal retains the active run", Disposal),
        ("RPC session cancellation callback cleanup", CancellationCleanup),
        ("RPC session callback shutdown avoids self-wait", CallbackShutdown),
        ("RPC session independent histories and cancellation", Independent),
        ("RPC session native provider cleanup and deadlines", NativeProvider),
        ("RPC session detached commands and protocol values", DetachedValues),
        ("RPC session input validation and command errors", Validation)
    ];

    private static TaskCompletionSource Signal() => new(TaskCreationOptions.RunContinuationsAsynchronously);
    private static async Task Await(Task task) => await task.WaitAsync(TimeSpan.FromSeconds(3));
    private static ProviderTurn Turn(string text = "done") => new(text, [], [], []);
    private static JsonObject Prompt(string message = "go", string id = "prompt") =>
        new() { ["id"] = id, ["type"] = "prompt", ["message"] = message };
    private static JsonObject Command(string type) => new() { ["id"] = type, ["type"] = type };
    private static string Type(JsonObject value) => value["type"]!.GetValue<string>();
    private static bool Terminal(JsonObject value) => Type(value) == "agent_end";
    private static string Status(JsonObject value) => value["status"]!.GetValue<string>();
    private static JsonObject[] Events(ConcurrentQueue<JsonObject> output) => output.Where(value => value.ContainsKey("sequence")).ToArray();
    private static ValueTask Discard(JsonObject _, CancellationToken? token) => ValueTask.CompletedTask;
    private static RpcEmitter Collect(ConcurrentQueue<JsonObject> output) => (value, _) => { output.Enqueue(value); return ValueTask.CompletedTask; };

    private static void State(RpcSession session, int messages, string status = "idle")
    {
        var state = session.GetState();
        Check.Equal(1, state["protocolVersion"]!.GetValue<int>());
        Check.Equal(status, Status(state));
        Check.Equal(messages, state["messageCount"]!.GetValue<int>());
        Check.Equal(status == "idle", state["runId"] is null);
    }

    private static async Task Complete(RpcSession session, string prompt = "go")
    {
        await session.HandleAsync(Prompt(prompt));
        Check.True(await session.WaitForIdleAsync().WaitAsync(TimeSpan.FromSeconds(3)), "External caller failed to await the worker.");
    }

    private static async Task SharedFixtures()
    {
        var fixture = JsonNode.Parse(await File.ReadAllTextAsync(Path.Combine(AppContext.BaseDirectory, "fixtures/rpc_v1.json")))!;
        foreach (var scenario in fixture["scenarios"]!.AsArray().Where(value => value?["expected"] is not null))
        {
            using var provider = new ScriptedProvider((_, _, _) => throw new InvalidOperationException("Idle command reached provider."));
            ConcurrentQueue<JsonObject> output = [];
            await using var session = new RpcSession(new Agent(provider, "fixture"), Collect(output));
            foreach (var command in scenario!["commands"]!.AsArray()) await session.HandleAsync(command);
            var actual = new JsonArray(output.Select(value => (JsonNode)value.DeepClone()).ToArray());
            Check.True(JsonNode.DeepEquals(scenario["expected"], actual), $"Shared scenario failed: {scenario["name"]}");
        }
    }

    private static async Task Acknowledgement()
    {
        foreach (bool fail in new[] { false, true })
        {
            var entered = Signal();
            var release = Signal();
            int calls = 0;
            using var provider = new ScriptedProvider((_, _, _) => { Interlocked.Increment(ref calls); return Task.FromResult(Turn()); });
            ConcurrentQueue<JsonObject> output = [];
            await using var session = new RpcSession(new Agent(provider, "fixture"), async (value, token) =>
            {
                output.Enqueue(value);
                if (Type(value) != "response" || token is null) return;
                Check.True(token.Value.CanBeCanceled, "Run acknowledgement lacks cancellation.");
                entered.TrySetResult();
                await release.Task;
                if (fail) throw new IOException("acknowledgement failed");
            });
            await session.HandleAsync(Prompt());
            try
            {
                await Await(entered.Task);
                Check.Equal(0, Volatile.Read(ref calls));
                State(session, 0, "running");
                await session.HandleAsync(Prompt("busy", "second"));
                Check.Equal("busy", output.Last()["error"]!["code"]!.GetValue<string>());
                Check.Equal(0, Events(output).Length);
            }
            finally { release.TrySetResult(); await session.WaitForIdleAsync(); }
            Check.Equal(fail ? 0 : 1, calls);
            State(session, fail ? 0 : 2);
            Check.Equal(fail ? 0 : 3, Events(output).Length);
            if (!fail) Check.Equal("done", Events(output)[1]["delta"]!.GetValue<string>());
            Check.Equal("response", Type(output.First()));
        }
    }

    private static async Task CancelAcknowledgement()
    {
        foreach (bool throws in new[] { false, true })
        {
            var entered = Signal();
            var cancelled = Signal();
            var release = Signal();
            int calls = 0;
            using var provider = new ScriptedProvider((_, _, _) => { calls++; return Task.FromResult(Turn()); });
            ConcurrentQueue<JsonObject> output = [];
            await using var session = new RpcSession(new Agent(provider, "fixture"), async (value, token) =>
            {
                output.Enqueue(value);
                if (Type(value) != "response" || token is null) return;
                using var registration = token.Value.Register(() => cancelled.TrySetResult());
                entered.TrySetResult();
                await release.Task;
                if (throws) token.Value.ThrowIfCancellationRequested();
            });
            await session.HandleAsync(Prompt());
            try
            {
                await Await(entered.Task);
                await session.HandleAsync(Command("abort"));
                await Await(cancelled.Task);
                State(session, 0, "running");
                Check.True(!session.WaitForIdleAsync().IsCompleted, "Cancellation abandoned an acknowledgement callback.");
                Check.Equal(0, calls);
            }
            finally { release.TrySetResult(); await session.WaitForIdleAsync(); }
            State(session, 0);
            Check.Equal(0, calls);
            Check.Equal(throws ? 0 : 2, Events(output).Length);
            if (!throws) Check.Equal("aborted", Status(output.Last()));
        }
    }

    private static async Task Commit()
    {
        var entered = Signal();
        var release = Signal();
        List<ProviderRequest> requests = [];
        using var provider = new ScriptedProvider(async (request, emit, token) =>
        {
            requests.Add(request);
            if (emit is not null) await emit("done\ncafé", token);
            return Turn("done\ncafé");
        });
        ConcurrentQueue<JsonObject> output = [];
        await using var session = new RpcSession(new Agent(provider, "fixture"), async (value, token) =>
        {
            output.Enqueue(value);
            if (Terminal(value)) { entered.TrySetResult(); await release.Task; }
        });
        await session.HandleAsync(Prompt("first"));
        try
        {
            await Await(entered.Task);
            State(session, 0, "running");
            await session.HandleAsync(Prompt("busy"));
            Check.Equal("busy", output.Last()["error"]!["code"]!.GetValue<string>());
            Check.Equal("done\ncafé", Events(output)[^1]["text"]!.GetValue<string>());
        }
        finally { release.TrySetResult(); await session.WaitForIdleAsync(); }
        State(session, 2);
        var events = Events(output);
        Check.Equal(3, events.Length);
        for (int index = 0; index < events.Length; index++)
        {
            Check.Equal((long)index + 1, events[index]["sequence"]!.GetValue<long>());
            Check.Equal(events[0]["runId"]!.GetValue<string>(), events[index]["runId"]!.GetValue<string>());
        }
        await Complete(session, "second");
        State(session, 4);
        Check.Equal(3, requests[^1].History.Count);
        Check.Equal("first", ((UserMessage)requests[^1].History[0]).Text);
        Check.Equal("done\ncafé", ((AssistantMessage)requests[^1].History[1]).Text);
        Check.True(!provider.Disposed, "Session disposed the caller's provider.");
    }

    private static async Task Rollback()
    {
        foreach (string mode in new[] { "provider", "consumer", "cancel", "tool" })
        {
            List<ProviderRequest> requests = [];
            using var provider = new ScriptedProvider((request, _, _) =>
            {
                requests.Add(request);
                if (requests.Count == 2)
                {
                    if (mode == "provider") throw new ProviderException("provider failed");
                    if (mode == "tool") return Task.FromResult(new ProviderTurn("", [new ToolCall("call", "unknown", System.Text.Json.JsonSerializer.SerializeToElement(new { }))], [], []));
                }
                return Task.FromResult(Turn());
            });
            ConcurrentQueue<JsonObject> output = [];
            RpcSession? owner = null;
            await using var session = new RpcSession(new Agent(provider, "fixture"), (value, _) =>
            {
                output.Enqueue(value);
                if (requests.Count == 2 && Terminal(value))
                {
                    if (mode == "consumer") throw new IOException("final delivery failed");
                    if (mode == "cancel") owner!.Cancel();
                }
                return ValueTask.CompletedTask;
            });
            owner = session;
            await Complete(session, "seed");
            await Complete(session, "failed turn");
            State(session, 2);
            await Complete(session, "next");
            State(session, 4);
            Check.Equal(3, requests[^1].History.Count);
            Check.True(requests[^1].History.OfType<UserMessage>().All(message => message.Text != "failed turn"), "Failed run contaminated history.");
            Check.Equal(3, output.Count(Terminal));
        }
    }

    private static async Task Abort()
    {
        var entered = Signal();
        var cleaning = Signal();
        var release = Signal();
        using var provider = new ScriptedProvider(async (_, _, token) =>
        {
            entered.TrySetResult();
            try { await Task.Delay(Timeout.Infinite, token); }
            finally { cleaning.TrySetResult(); await release.Task; }
            return Turn();
        });
        ConcurrentQueue<JsonObject> output = [];
        await using var session = new RpcSession(new Agent(provider, "fixture"), Collect(output));
        await session.HandleAsync(Prompt());
        try
        {
            await Await(entered.Task);
            string runId = session.GetState()["runId"]!.GetValue<string>();
            await session.HandleAsync(Command("abort"));
            var response = output.Last();
            Check.True(response["data"]!["accepted"]!.GetValue<bool>(), "Abort was not accepted.");
            Check.Equal(runId, response["data"]!["runId"]!.GetValue<string>());
            await Await(cleaning.Task);
            State(session, 0, "running");
            Check.Equal(runId, session.Cancel());
            Check.Equal(0, output.Count(Terminal));
        }
        finally { release.TrySetResult(); await session.WaitForIdleAsync(); }
        State(session, 0);
        Check.Equal("aborted", Status(output.Last()));
        Check.True(session.Cancel() is null, "Idle cancellation returned an active run.");
    }

    private static async Task Reset()
    {
        var entered = Signal();
        var release = Signal();
        var cancelled = Signal();
        int calls = 0;
        using var provider = new ScriptedProvider((_, _, _) => { calls++; return Task.FromResult(Turn()); });
        ConcurrentQueue<JsonObject> output = [];
        await using var session = new RpcSession(new Agent(provider, "fixture"), async (value, token) =>
        {
            output.Enqueue(value);
            if (calls == 2 && Terminal(value))
            {
                using var registration = token!.Value.Register(() => cancelled.TrySetResult());
                entered.TrySetResult();
                await release.Task;
            }
        });
        await Complete(session, "seed");
        await session.HandleAsync(Prompt("held"));
        await Await(entered.Task);
        var reset = session.HandleAsync(Command("new_session")).AsTask();
        try
        {
            await Await(cancelled.Task);
            State(session, 0, "running");
            Check.True(!reset.IsCompleted, "Reset returned while the old callback was active.");
            await session.HandleAsync(Command("new_session"));
            Check.Equal("busy", output.Last()["error"]!["code"]!.GetValue<string>());
            await session.HandleAsync(Prompt("busy"));
            Check.Equal("busy", output.Last()["error"]!["code"]!.GetValue<string>());
        }
        finally { release.TrySetResult(); await Await(reset); }
        Check.True(output.Last()["data"]!["abortedRun"]!.GetValue<bool>(), "Reset missed its run.");
        State(session, 0);
        await Complete(session, "fresh");
        State(session, 2);
        await session.HandleAsync(Command("new_session"));
        Check.True(!output.Last()["data"]!["abortedRun"]!.GetValue<bool>(), "Idle reset reported a run.");
        State(session, 0);
    }

    private static async Task Disposal()
    {
        var entered = Signal();
        var release = Signal();
        using var provider = new ScriptedProvider((_, _, _) => Task.FromResult(Turn()));
        ConcurrentQueue<JsonObject> output = [];
        var session = new RpcSession(new Agent(provider, "fixture"), async (value, _) =>
        {
            output.Enqueue(value);
            if (Terminal(value)) { entered.TrySetResult(); await release.Task; }
        });
        await session.HandleAsync(Prompt());
        await Await(entered.Task);
        string runId = session.GetState()["runId"]!.GetValue<string>();
        Check.Equal(runId, session.Stop());
        var disposed = session.DisposeAsync().AsTask();
        try
        {
            Check.True(!disposed.IsCompleted, "Disposal abandoned callback delivery.");
            foreach (var request in new[] { Prompt(), Command("new_session") })
            {
                await session.HandleAsync(request);
                Check.Equal("session_closed", output.Last()["error"]!["code"]!.GetValue<string>());
            }
            State(session, 0, "running");
        }
        finally { release.TrySetResult(); await Await(disposed); }
        State(session, 0);
        await session.DisposeAsync();
        Check.True(!provider.Disposed, "Disposal closed a borrowed provider.");
    }

    private static async Task CancellationCleanup()
    {
        foreach (bool cancelled in new[] { false, true })
        {
            var entered = Signal();
            var ready = Signal();
            var providerRelease = Signal();
            using var release = new ManualResetEventSlim();
            using var provider = new ScriptedProvider(async (_, _, _) =>
            {
                ready.TrySetResult();
                if (cancelled) await providerRelease.Task;
                return Turn();
            });
            RpcSession? owner = null;
            await using var session = new RpcSession(new Agent(provider, "fixture"), (value, token) =>
            {
                if (Type(value) == "response" && token is not null)
                    token.Value.Register(() =>
                    {
                        State(owner!, 0, "running");
                        entered.TrySetResult();
                        if (!release.Wait(TimeSpan.FromSeconds(3))) throw new TimeoutException("Cancellation callback was not released.");
                    });
                return ValueTask.CompletedTask;
            });
            owner = session;
            await session.HandleAsync(Prompt());
            try
            {
                await Await(ready.Task);
                if (cancelled) session.Cancel();
                providerRelease.TrySetResult();
                await Await(entered.Task);
                var waiting = session.WaitForIdleAsync();
                Check.True(!waiting.IsCompleted, "Session ignored pending cancellation callbacks.");
                State(session, 0, "running");
            }
            finally { release.Set(); providerRelease.TrySetResult(); await session.WaitForIdleAsync(); }
            State(session, cancelled ? 0 : 2);
        }
        using var success = new ScriptedProvider((_, _, _) => Task.FromResult(Turn()));
        await using var brokenCleanup = new RpcSession(new Agent(success, "fixture"), (value, token) =>
        {
            if (Type(value) == "response" && token is not null)
                token.Value.Register(() => throw new InvalidOperationException("release failed"));
            return ValueTask.CompletedTask;
        });
        await Complete(brokenCleanup);
        State(brokenCleanup, 0);
    }

    private static async Task CallbackShutdown()
    {
        foreach (bool reset in new[] { false, true })
        {
            bool called = false;
            using var provider = new ScriptedProvider((_, _, _) => Task.FromResult(Turn()));
            RpcSession? owner = null;
            await using var session = new RpcSession(new Agent(provider, "fixture"), async (value, _) =>
            {
                if (!Terminal(value) || called) return;
                Check.True(!await owner!.WaitForIdleAsync(), "Callback tried to await itself.");
                if (reset) await owner.HandleAsync(Command("new_session"));
                else await owner.DisposeAsync();
                called = true;
            });
            owner = session;
            await Complete(session);
            Check.True(called, "Callback shutdown failed.");
            State(session, 0);
            if (reset) { await Complete(session); State(session, 2); }
        }
    }

    private static async Task Independent()
    {
        int calls = 0;
        var entered = Signal();
        var release = Signal();
        List<ProviderRequest> requests = [];
        using var provider = new ScriptedProvider(async (request, _, token) =>
        {
            lock (requests) requests.Add(request);
            if (Interlocked.Increment(ref calls) == 2) entered.TrySetResult();
            await release.Task;
            token.ThrowIfCancellationRequested();
            return Turn();
        });
        var agent = new Agent(provider, "fixture");
        await using var first = new RpcSession(agent, Discard);
        await using var second = new RpcSession(agent, Discard);
        await first.HandleAsync(Prompt("first"));
        await second.HandleAsync(Prompt("second"));
        try
        {
            await Await(entered.Task);
            Check.True(first.GetState()["runId"]!.GetValue<string>() != second.GetState()["runId"]!.GetValue<string>(), "Sessions shared a run ID.");
            first.Cancel();
        }
        finally { release.TrySetResult(); await first.WaitForIdleAsync(); await second.WaitForIdleAsync(); }
        State(first, 0);
        State(second, 2);
        await Complete(first, "fresh");
        Check.Equal(1, requests[^1].History.Count);
        await Complete(second, "next");
        Check.Equal(3, requests[^1].History.Count);
        Check.Equal("second", ((UserMessage)requests[^1].History[0]).Text);
    }

    private static async Task NativeProvider()
    {
        foreach (bool anthropic in new[] { false, true })
        {
            using var fixture = new HttpFixture();
            var seed = fixture.Enqueue(HttpFixture.Wire(HttpFixture.Sse(anthropic ? "anthropic_text" : "openai_text")));
            var blocked = fixture.Enqueue(HttpFixture.Wire("data: {\"type\":\"ping\"}\n\n", ending: false), eof: false);
            using var provider = ProviderChecks.Provider(anthropic, fixture.Client);
            ConcurrentQueue<JsonObject> output = [];
            await using var session = new RpcSession(new Agent(provider, "fixture"), Collect(output));
            await Complete(session, "seed");
            State(session, 2);
            Check.True(seed.Closed.Task.IsCompleted, "Completed session retained native response.");
            await session.HandleAsync(Prompt("wait"));
            try { await Await(blocked.Waiting.Task); }
            finally { session.Cancel(); await session.WaitForIdleAsync(); }
            State(session, 2);
            Check.True(blocked.Closed.Task.IsCompleted, "Cancelled session retained native response.");
            Check.Equal("aborted", Status(output.Last()));
        }
        using var slow = new HttpFixture();
        var headers = slow.Enqueue([], eof: false);
        using var deadline = ProviderChecks.Provider(false, slow.Client, TimeSpan.FromMilliseconds(100));
        ConcurrentQueue<JsonObject> errors = [];
        await using var failed = new RpcSession(new Agent(deadline, "fixture"), Collect(errors));
        await Complete(failed);
        State(failed, 0);
        Check.True(headers.Closed.Task.IsCompleted, "Deadline retained native response.");
        Check.Equal("failed", Status(errors.Last()));
    }

    private static async Task DetachedValues()
    {
        var entered = Signal();
        var release = Signal();
        ProviderRequest? received = null;
        using var provider = new ScriptedProvider((request, _, _) => { received = request; return Task.FromResult(Turn()); });
        ConcurrentQueue<JsonObject> output = [];
        await using var session = new RpcSession(new Agent(provider, "fixture"), async (value, token) =>
        {
            output.Enqueue((JsonObject)value.DeepClone());
            if (Type(value) == "response" && token is not null)
            {
                value["data"]!["runId"] = "changed";
                entered.TrySetResult();
                await release.Task;
            }
            else if (Terminal(value)) value["text"] = "changed";
        });
        var command = Prompt("original", "original-id");
        await session.HandleAsync(command);
        try
        {
            command["message"] = "changed";
            command["id"] = "changed";
            await Await(entered.Task);
            session.GetState()["messageCount"] = 999;
            State(session, 0, "running");
        }
        finally { release.TrySetResult(); await session.WaitForIdleAsync(); }
        State(session, 2);
        Check.Equal("original", ((UserMessage)received!.History[0]).Text);
        Check.Equal("original-id", output.First()["id"]!.GetValue<string>());
        Check.Equal(output.First()["data"]!["runId"]!.GetValue<string>(), Events(output)[0]["runId"]!.GetValue<string>());
        await Complete(session, "next");
        Check.Equal("done", ((AssistantMessage)received.History[1]).Text);
    }

    private static async Task Validation()
    {
        using var provider = new ScriptedProvider((_, _, _) => throw new InvalidOperationException("Invalid input reached provider."));
        ConcurrentQueue<JsonObject> output = [];
        await using var session = new RpcSession(new Agent(provider, "fixture"), Collect(output));
        JsonNode?[] invalidCommands =
        [
            null,
            new JsonArray(),
            JsonValue.Create(7),
            new JsonObject { ["id"] = null, ["type"] = "get_state" },
            new JsonObject { ["type"] = "prompt", ["message"] = false },
            new JsonObject { ["id"] = "", ["type"] = 7 },
            new JsonObject { ["type"] = "prompt" }
        ];
        foreach (var invalid in invalidCommands)
        {
            await session.HandleAsync(invalid);
            Check.Equal("invalid_command", output.Last()["error"]!["code"]!.GetValue<string>());
        }
        JsonNode[] invalidJson =
        [
            new JsonObject { ["type"] = "get_state", ["extra"] = "\ud800" },
            new JsonObject { ["\udc00"] = 1 },
            JsonNode.Parse("{\"type\":\"get_state\",\"extra\":\"\\ud800\"}")!,
            JsonNode.Parse("{\"type\":\"get_state\",\"type\":\"prompt\"}")!
        ];
        foreach (var invalid in invalidJson)
        {
            await session.HandleAsync(invalid);
            Check.Equal("invalid_json", output.Last()["error"]!["code"]!.GetValue<string>());
        }
        JsonNode nested = new JsonObject();
        for (int depth = 0; depth < 101; depth++) nested = new JsonObject { ["child"] = nested };
        await session.HandleAsync(nested);
        Check.Equal("invalid_json", output.Last()["error"]!["code"]!.GetValue<string>());
        State(session, 0);
        await session.HandleAsync(new JsonObject { ["id"] = "", ["type"] = "get_state" });
        Check.Equal("", output.Last()["id"]!.GetValue<string>());
        var failure = new IOException("command delivery failed");
        await using var broken = new RpcSession(new Agent(provider, "fixture"), (_, _) => throw failure);
        Check.True(ReferenceEquals(failure, await Check.ThrowsAsync<IOException>(() => broken.HandleAsync(Command("get_state")).AsTask())), "Command delivery error was replaced.");
        await Check.ThrowsAsync<ArgumentNullException>(() => Task.FromResult(new RpcSession(null!, Discard)));
        await Check.ThrowsAsync<ArgumentNullException>(() => Task.FromResult(new RpcSession(new Agent(provider, "fixture"), null!)));
    }
}
