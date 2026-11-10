using System.Text.Json;

namespace TinyAgent.Checks;

internal static class CoreChecks
{
    internal static IEnumerable<(string Name, Func<Task> Run)> Cases =>
    [
        ("Core native providers and ordered events", NativeRuns),
        ("Core immutable completed history and output", History),
        ("Core typed protocol events", Events),
        ("Core provider failures and unregistered tools", Failures),
        ("Core cancellation before provider work", PreCancellation),
        ("Core cancelled providers cannot return success", CancellationWins),
        ("Core native cancellation releases headers and body", NativeCancellation),
        ("Core consumer failures survive provider wrapping", ConsumerFailure),
        ("Core terminal delivery can prevent completion", TerminalDelivery),
        ("Core held callbacks and provider deadlines", HeldCallbacks),
        ("Core cancellation callbacks finish before terminal delivery", CancellationCleanup),
        ("Core asynchronous response cleanup precedes terminal delivery", NativeCleanup),
        ("Core concurrent runs remain independent", ConcurrentRuns),
        ("Core input and Unicode validation", Validation)
    ];

    private static ProviderTurn Turn(string text = "done")
    {
        using var replay = JsonDocument.Parse("{\"signature\":\"keep\"}");
        using var output = JsonDocument.Parse("{\"value\":1}");
        return new ProviderTurn(text, [], [replay.RootElement], [output.RootElement]);
    }

    private static ValueTask Delta(TextDeltaHandler? emit, string text, CancellationToken token) =>
        emit?.Invoke(text, token) ?? ValueTask.CompletedTask;

    private static AgentEventHandler Collect(List<AgentEvent> events) => (value, _) =>
    {
        events.Add(value);
        return ValueTask.CompletedTask;
    };

    private static void Ordered(List<AgentEvent> events, string status)
    {
        Check.True(events[0] is AgentEvent.Start, "The first event was not agent_start.");
        Check.Equal(status, events[^1].ToJson()["status"]!.GetValue<string>());
        Check.Equal(1, events.Count(value => value is AgentEvent.Completed or AgentEvent.Failed or AgentEvent.Aborted));
        for (int index = 0; index < events.Count; index++)
        {
            Check.Equal((long)index + 1, events[index].Sequence);
            Check.Equal(events[0].RunId, events[index].RunId);
        }
    }

    private static async Task NativeRuns()
    {
        foreach (bool anthropic in new[] { false, true })
        {
            using var fixture = new HttpFixture();
            var stream = fixture.Enqueue(HttpFixture.Wire(HttpFixture.Sse(anthropic ? "anthropic_text" : "openai_text"), ending: false), eof: false);
            using var provider = ProviderChecks.Provider(anthropic, fixture.Client);
            var agent = new Agent(provider, "fixture-model", "instructions");
            List<AgentEvent> events = [];
            var result = await agent.RunAsync("hello", onEvent: Collect(events), runId: "r_fixture");
            Ordered(events, "completed");
            Check.Equal("r_fixture", events[0].RunId);
            Check.Equal("done\ncafé", result.Text);
            Check.Equal("done\ncafé", string.Concat(events.OfType<AgentEvent.TextDelta>().Select(value => value.Delta)));
            Check.Equal(2, result.History.Count);
            Check.True(stream.Closed.Task.IsCompleted, "Core returned before native response cleanup.");
            Check.Equal("fixture-model", stream.Request().Body.GetProperty("model").GetString());
        }
    }

    private static async Task History()
    {
        List<ProviderRequest> requests = [];
        using var provider = new ScriptedProvider((request, _, _) =>
        {
            requests.Add(request);
            return Task.FromResult(Turn("answer " + requests.Count));
        });
        var agent = new Agent(provider, "fixture");
        var first = await agent.RunAsync("first");
        var input = first.History.ToList();
        var second = await agent.RunAsync("second", input);
        input.Clear();
        Check.Equal(2, first.History.Count);
        Check.Equal(4, second.History.Count);
        Check.Equal("answer 2", second.Text);
        var replay = (AssistantMessage)requests[1].History[1];
        Check.Equal("keep", replay.Replay[0].GetProperty("signature").GetString());
        Check.Equal(1, second.Output[0].GetProperty("value").GetInt32());
        await Check.ThrowsAsync<NotSupportedException>(() =>
        {
            ((IList<Message>)first.History).Clear();
            return Task.CompletedTask;
        });
        Check.True(!provider.Disposed, "Agent disposed its caller-owned provider.");
    }

    private static async Task Events()
    {
        var text = new AgentEvent.TextDelta("r", 2, "café\n");
        var json = text.ToJson();
        json["delta"] = "changed";
        Check.Equal("café\n", text.ToJson()["delta"]!.GetValue<string>());
        Check.Equal("provider_error", new AgentEvent.Failed("r", 3, "failed").ToJson()["error"]!["code"]!.GetValue<string>());
        Check.True(!new AgentEvent.Aborted("r", 3).ToJson().ContainsKey("text"), "Aborted event includes final text.");
        await Check.ThrowsAsync<ArgumentException>(() => Task.FromResult(new AgentEvent.Start("", 1)));
        await Check.ThrowsAsync<ArgumentOutOfRangeException>(() => Task.FromResult(new AgentEvent.Start("r", 0)));
    }

    private static async Task Failures()
    {
        foreach (bool partial in new[] { false, true })
        {
            var failure = new ProviderException("broken provider");
            using var provider = new ScriptedProvider(async (_, text, token) =>
            {
                if (partial) await Delta(text, "partial", token);
                throw failure;
            });
            List<Message> history = [new UserMessage("old")];
            List<AgentEvent> events = [];
            var error = await Check.ThrowsAsync<ProviderException>(() => new Agent(provider, "fixture")
                .RunAsync("new", history, Collect(events)));
            Check.True(ReferenceEquals(failure, error), "Provider failure was replaced.");
            Check.Equal(1, history.Count);
            Ordered(events, "failed");
        }
        using var arguments = JsonDocument.Parse("{}");
        using var tools = new ScriptedProvider((_, _, _) => Task.FromResult(new ProviderTurn("",
            [new ToolCall("c", "echo", arguments.RootElement)], [], [])));
        List<AgentEvent> calls = [];
        await Check.ThrowsAsync<ProviderException>(() => new Agent(tools, "fixture").RunAsync("go", onEvent: Collect(calls)));
        Ordered(calls, "failed");
        Check.Equal(2, calls.Count);
    }

    private static async Task PreCancellation()
    {
        foreach (bool before in new[] { false, true })
        {
            using var parent = new CancellationTokenSource();
            if (before) parent.Cancel();
            using var provider = new ScriptedProvider((_, _, _) => throw new InvalidOperationException("Provider ran after cancellation."));
            List<AgentEvent> events = [];
            await Check.ThrowsAsync<OperationCanceledException>(() => new Agent(provider, "fixture").RunAsync("go", onEvent: (value, _) =>
            {
                events.Add(value);
                if (value is AgentEvent.Start) parent.Cancel();
                return ValueTask.CompletedTask;
            }, cancellationToken: parent.Token));
            Ordered(events, "aborted");
        }
    }

    private static async Task CancellationWins()
    {
        foreach (bool fails in new[] { false, true })
        {
            using var parent = new CancellationTokenSource();
            using var provider = new ScriptedProvider((_, _, _) =>
            {
                parent.Cancel();
                if (fails) throw new IOException("incidental failure");
                return Task.FromResult(Turn());
            });
            List<AgentEvent> events = [];
            await Check.ThrowsAsync<OperationCanceledException>(() => new Agent(provider, "fixture")
                .RunAsync("go", onEvent: Collect(events), cancellationToken: parent.Token));
            Ordered(events, "aborted");
        }
    }

    private static async Task NativeCancellation()
    {
        foreach (bool anthropic in new[] { false, true })
        {
            foreach (bool headers in new[] { false, true })
            {
                using var fixture = new HttpFixture();
                var stream = fixture.Enqueue(headers ? [] : HttpFixture.Wire("data: {\"type\":\"ping\"}\n\n", ending: false), eof: false);
                using var provider = ProviderChecks.Provider(anthropic, fixture.Client);
                using var parent = new CancellationTokenSource();
                List<AgentEvent> events = [];
                var task = new Agent(provider, "fixture").RunAsync("go", onEvent: Collect(events), cancellationToken: parent.Token);
                try { await stream.Waiting.Task.WaitAsync(TimeSpan.FromSeconds(2)); }
                finally { parent.Cancel(); }
                await Check.ThrowsAsync<OperationCanceledException>(() => task.WaitAsync(TimeSpan.FromSeconds(2)));
                Ordered(events, "aborted");
                Check.True(stream.Closed.Task.IsCompleted, "Cancelled core retained native I/O.");
            }
        }
    }

    private static async Task ConsumerFailure()
    {
        var failure = new IOException("consumer left");
        CancellationToken runToken = default;
        using var scripted = new ScriptedProvider(async (_, text, token) =>
        {
            runToken = token;
            try { await Delta(text, "partial", token); }
            catch (IOException) { /* A custom provider cannot turn a failed consumer into success. */ }
            return Turn();
        });
        List<AgentEvent> events = [];
        var error = await Check.ThrowsAsync<IOException>(() => new Agent(scripted, "fixture").RunAsync("go", onEvent: (value, _) =>
        {
            events.Add(value);
            return value is AgentEvent.TextDelta ? ValueTask.FromException(failure) : ValueTask.CompletedTask;
        }));
        Check.True(ReferenceEquals(failure, error) && runToken.IsCancellationRequested, "Consumer failure or cleanup was lost.");
        Check.Equal(2, events.Count);
        foreach (bool anthropic in new[] { false, true })
        {
            using var fixture = new HttpFixture();
            var stream = fixture.Enqueue(HttpFixture.Wire(HttpFixture.Sse(anthropic ? "anthropic_text" : "openai_text")));
            using var provider = ProviderChecks.Provider(anthropic, fixture.Client);
            error = await Check.ThrowsAsync<IOException>(() => new Agent(provider, "fixture").RunAsync("go", onEvent: (value, _) =>
                value is AgentEvent.TextDelta ? ValueTask.FromException(failure) : ValueTask.CompletedTask));
            Check.True(ReferenceEquals(failure, error), "Native provider wrapping hid the consumer error.");
            Check.True(stream.Closed.Task.IsCompleted, "Failed consumer retained the native body.");
        }
    }

    private static async Task TerminalDelivery()
    {
        foreach (bool cancel in new[] { false, true })
        {
            using var parent = new CancellationTokenSource();
            using var provider = new ScriptedProvider((_, _, _) => Task.FromResult(Turn()));
            var failure = new IOException("terminal write failed");
            List<AgentEvent> events = [];
            var task = new Agent(provider, "fixture").RunAsync("go", onEvent: (value, _) =>
            {
                events.Add(value);
                if (value is AgentEvent.Completed)
                {
                    if (cancel) parent.Cancel();
                    else throw failure;
                }
                return ValueTask.CompletedTask;
            }, cancellationToken: parent.Token);
            if (cancel) await Check.ThrowsAsync<OperationCanceledException>(() => task);
            else Check.True(ReferenceEquals(failure, await Check.ThrowsAsync<IOException>(() => task)), "Terminal error was replaced.");
            Ordered(events, "completed");
        }
    }

    private static async Task HeldCallbacks()
    {
        foreach (bool deadline in new[] { false, true })
        {
            using var fixture = new HttpFixture();
            var stream = fixture.Enqueue(HttpFixture.Wire(HttpFixture.Sse("openai_text")));
            using var provider = ProviderChecks.Provider(false, fixture.Client,
                deadline ? TimeSpan.FromMilliseconds(100) : TimeSpan.FromSeconds(3));
            using var parent = new CancellationTokenSource();
            var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            var cancelled = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            List<AgentEvent> events = [];
            var task = new Agent(provider, "fixture").RunAsync("go", onEvent: async (value, token) =>
            {
                events.Add(value);
                if (value is not AgentEvent.TextDelta) return;
                using var registration = token.Register(() => cancelled.TrySetResult());
                entered.TrySetResult();
                await release.Task;
            }, cancellationToken: parent.Token);
            try
            {
                await entered.Task.WaitAsync(TimeSpan.FromSeconds(2));
                if (!deadline) parent.Cancel();
                await cancelled.Task.WaitAsync(TimeSpan.FromSeconds(2));
                Check.True(!task.IsCompleted, "Core abandoned a held callback.");
            }
            finally { release.TrySetResult(); }
            if (deadline) await Check.ThrowsAsync<ProviderException>(() => task);
            else await Check.ThrowsAsync<OperationCanceledException>(() => task);
            Ordered(events, deadline ? "failed" : "aborted");
            Check.True(stream.Closed.Task.IsCompleted, "Held callback retained HTTP resources.");
        }
    }

    private static async Task CancellationCleanup()
    {
        foreach (bool parentCancels in new[] { false, true })
        {
            using var parent = new CancellationTokenSource();
            using var release = new ManualResetEventSlim();
            var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            var finishProvider = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            bool cleaned = false;
            using var provider = new ScriptedProvider(async (_, _, token) =>
            {
                token.Register(() =>
                {
                    entered.TrySetResult();
                    if (!release.Wait(TimeSpan.FromSeconds(3))) throw new TimeoutException("Cleanup did not release.");
                    cleaned = true;
                });
                if (parentCancels) await finishProvider.Task;
                return Turn();
            });
            List<AgentEvent> events = [];
            var task = new Agent(provider, "fixture").RunAsync("go", onEvent: (value, _) =>
            {
                if (value is AgentEvent.Completed or AgentEvent.Failed or AgentEvent.Aborted)
                    Check.True(cleaned, "Terminal delivery preceded cancellation cleanup.");
                events.Add(value);
                return ValueTask.CompletedTask;
            }, cancellationToken: parent.Token);
            try
            {
                if (parentCancels) { parent.Cancel(); finishProvider.TrySetResult(); }
                await entered.Task.WaitAsync(TimeSpan.FromSeconds(2));
                Check.True(!task.IsCompleted, "Run returned while cancellation callbacks remained active.");
            }
            finally { release.Set(); finishProvider.TrySetResult(); }
            if (parentCancels) await Check.ThrowsAsync<OperationCanceledException>(() => task);
            else Check.Equal("done", (await task).Text);
            Ordered(events, parentCancels ? "aborted" : "completed");
        }
        using var broken = new ScriptedProvider((_, _, token) =>
        {
            token.Register(() => throw new InvalidOperationException("cleanup failed"));
            return Task.FromResult(Turn());
        });
        List<AgentEvent> failed = [];
        await Check.ThrowsAsync<AggregateException>(() => new Agent(broken, "fixture").RunAsync("go", onEvent: Collect(failed)));
        Ordered(failed, "failed");
    }

    private static async Task NativeCleanup()
    {
        foreach (bool cancel in new[] { false, true })
        {
            await using var stream = new CleanupStream(System.Text.Encoding.UTF8.GetBytes(HttpFixture.Sse("openai_text")));
            using var client = new HttpClient(new ContentHandler(stream)) { Timeout = Timeout.InfiniteTimeSpan };
            using var provider = ProviderChecks.Provider(false, client);
            using var parent = new CancellationTokenSource();
            List<AgentEvent> events = [];
            var task = new Agent(provider, "fixture").RunAsync("go", onEvent: (value, _) =>
            {
                if (value is AgentEvent.Completed or AgentEvent.Aborted)
                    Check.True(stream.Finished, "Terminal event preceded native async cleanup.");
                events.Add(value);
                return ValueTask.CompletedTask;
            }, cancellationToken: parent.Token);
            try
            {
                await stream.Started.Task.WaitAsync(TimeSpan.FromSeconds(2));
                if (cancel) parent.Cancel();
                Check.True(!task.IsCompleted, "Core abandoned native async cleanup.");
            }
            finally { stream.Release.TrySetResult(); }
            if (cancel) await Check.ThrowsAsync<OperationCanceledException>(() => task);
            else Check.Equal("done\ncafé", (await task).Text);
            Ordered(events, cancel ? "aborted" : "completed");
        }
    }

    private static async Task ConcurrentRuns()
    {
        var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        int count = 0;
        using var provider = new ScriptedProvider(async (request, _, token) =>
        {
            if (Interlocked.Increment(ref count) == 2) entered.TrySetResult();
            await release.Task;
            token.ThrowIfCancellationRequested();
            return Turn(((UserMessage)request.History[^1]).Text);
        });
        var agent = new Agent(provider, "fixture");
        using var parent = new CancellationTokenSource();
        List<AgentEvent> firstEvents = [];
        List<AgentEvent> secondEvents = [];
        var first = agent.RunAsync("first", onEvent: Collect(firstEvents), cancellationToken: parent.Token);
        var second = agent.RunAsync("second", onEvent: Collect(secondEvents));
        try { await entered.Task.WaitAsync(TimeSpan.FromSeconds(2)); parent.Cancel(); }
        finally { release.TrySetResult(); }
        await Check.ThrowsAsync<OperationCanceledException>(() => first);
        Check.Equal("second", (await second).Text);
        Ordered(firstEvents, "aborted");
        Ordered(secondEvents, "completed");
        Check.True(firstEvents[0].RunId != secondEvents[0].RunId, "Independent runs reused an ID.");
    }

    private static async Task Validation()
    {
        int calls = 0;
        using var provider = new ScriptedProvider((_, _, _) => { calls++; return Task.FromResult(Turn()); });
        var agent = new Agent(provider, "fixture");
        await Check.ThrowsAsync<ArgumentException>(() => Task.FromResult(new Agent(provider, "")));
        await Check.ThrowsAsync<ArgumentException>(() => agent.RunAsync("\ud800"));
        await Check.ThrowsAsync<ArgumentException>(() => agent.RunAsync("go", runId: ""));
        await Check.ThrowsAsync<ArgumentException>(() => agent.RunAsync("go", [null!]));
        Check.Equal(0, calls);
        foreach (string text in new[] { "\ud800", "\udc00" })
        {
            using var malformed = new ScriptedProvider(async (_, emit, token) => { await Delta(emit, text, token); return Turn(); });
            List<AgentEvent> events = [];
            await Check.ThrowsAsync<ArgumentException>(() => new Agent(malformed, "fixture").RunAsync("go", onEvent: Collect(events)));
            Ordered(events, "failed");
        }
        using var empty = new ScriptedProvider((_, _, _) => Task.FromResult<ProviderTurn>(null!));
        await Check.ThrowsAsync<ProviderException>(() => new Agent(empty, "fixture").RunAsync("go"));
    }
}

internal sealed class ScriptedProvider(Func<ProviderRequest, TextDeltaHandler?, CancellationToken, Task<ProviderTurn>> run) : IProvider
{
    internal bool Disposed { get; private set; }
    public Task<ProviderTurn> StreamAsync(ProviderRequest request, TextDeltaHandler? onText = null, CancellationToken cancellationToken = default) =>
        run(request, onText, cancellationToken);
    public void Dispose() => Disposed = true;
}
