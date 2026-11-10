using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json.Nodes;

namespace TinyAgent.Checks;

// The CLI source is compiled unchanged into this host; providers use native HTTP over memory.
internal static class CliChecks
{
    internal static IEnumerable<(string Name, Func<Task> Run)> Cases =>
    [
        ("CLI streaming, replay, sequential Bash, and immutable history", Completed),
        ("CLI argument and Bash failures become recoverable observations", Recovery),
        ("CLI provider and callback failures retain prior history", Failures),
        ("CLI terminal-only text and cancellation", Terminal),
        ("CLI cancellation releases headers, body, and Bash", Cancellation),
        ("CLI console rollback, EOF, and caller ownership", ConsoleLoop),
        ("CLI native EOF and idle interrupt", NativeIdle),
        ("CLI native interrupt during HTTP", NativeRequest),
        ("CLI native interrupt joins Bash process cleanup", NativeBash),
        ("CLI native interrupt releases stalled stdout and stderr", NativeOutput),
        ("CLI native broken stdout releases provider I/O", NativeBroken)
    ];

    private static ValueTask Ignore(string _, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        return ValueTask.CompletedTask;
    }

    private static async Task Completed()
    {
        using var fixture = new HttpFixture();
        string marker = Path.Combine(AppContext.BaseDirectory, "cli-order-" + Guid.NewGuid().ToString("N"));
        var reasoning = new JsonObject { ["type"] = "reasoning", ["encrypted_content"] = "opaque" };
        string sse = HttpFixture.Events(new JsonObject
        { ["type"] = "response.output_text.delta", ["delta"] = "working " }) + CliFixture.Response("", reasoning,
            CliFixture.Call($"printf first > {CliFixture.Quote(marker)}", "first"),
            CliFixture.Call($"cat {CliFixture.Quote(marker)}; printf 'café\\n'", "second"));
        var first = fixture.Enqueue(HttpFixture.Wire(sse, ending: false), eof: false);
        var next = fixture.Enqueue(HttpFixture.Wire(HttpFixture.Sse("openai_text"), ending: false), eof: false);
        using var provider = ProviderChecks.Provider(false, fixture.Client);
        List<Message> previous = [new UserMessage("keep")];
        var output = new StringBuilder();
        var diagnostic = new StringBuilder();
        try
        {
            var result = await TinyAgentCli.TurnAsync(provider, previous, "go",
                (text, _) => { output.Append(text); return ValueTask.CompletedTask; },
                (text, _) => { diagnostic.Append(text); return ValueTask.CompletedTask; });
            Check.Equal(1, previous.Count);
            Check.Equal(6, result.Count);
            Check.Equal("working done\ncafé\n", output.ToString());
            Check.True(diagnostic.ToString().Contains("firstcafé\n", StringComparison.Ordinal), "Bash calls were not sequential.");
            Check.True(first.Closed.Task.IsCompleted && next.Closed.Task.IsCompleted, "CLI awaited HTTP EOF after terminal SSE.");
            var request = first.Request().Body;
            Check.True(request.GetProperty("stream").GetBoolean(), "CLI did not request streaming.");
            Check.True(!request.GetProperty("parallel_tool_calls").GetBoolean(), "Parallel calls were enabled.");
            Check.True(!request.GetProperty("store").GetBoolean(), "Provider storage was enabled.");
            Check.Equal("bash", request.GetProperty("tools")[0].GetProperty("name").GetString());
            Check.Equal("command", request.GetProperty("tools")[0].GetProperty("parameters").GetProperty("required")[0].GetString());
            var input = next.Request().Body.GetProperty("input");
            Check.Equal("opaque", input[2].GetProperty("encrypted_content").GetString());
            Check.Equal("second", input[6].GetProperty("call_id").GetString());
            Check.Equal("exit_code: 0\nstdout:\nfirstcafé\nstderr:\n", input[6].GetProperty("output").GetString());
            await Check.ThrowsAsync<NotSupportedException>(() => { ((IList<Message>)result).Clear(); return Task.CompletedTask; });
            var assistant = (AssistantMessage)result[2];
            await Check.ThrowsAsync<NotSupportedException>(() => { ((IList<ToolCall>)assistant.ToolCalls).Clear(); return Task.CompletedTask; });
        }
        finally { File.Delete(marker); }
    }

    private static async Task Recovery()
    {
        foreach (var arguments in new[] { "{}", "{\"command\":2}", "{\"command\":\"\"}", "{\"command\":\"exit 99\",\"extra\":true}", "{\"command\":\"yes x\"}", "{\"command\":\"\\u0000\"}" })
        {
            using var fixture = new HttpFixture();
            var call = CliFixture.Call("unused");
            call["arguments"] = arguments;
            fixture.Enqueue(HttpFixture.Wire(CliFixture.Response("", call, CliFixture.Call("printf recovered", "later"))));
            fixture.Enqueue(HttpFixture.Wire(CliFixture.Response("done")));
            using var provider = ProviderChecks.Provider(false, fixture.Client);
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            var result = await TinyAgentCli.TurnAsync(provider, [], "go", Ignore, Ignore, deadline.Token);
            var failure = (ToolResultMessage)result[2];
            Check.True(failure.IsError, "Invalid arguments or execution failure were accepted.");
            Check.True(Encoding.UTF8.GetByteCount(failure.Output) < 3000, "Tool failure was unbounded.");
            Check.True(((ToolResultMessage)result[3]).Output.Contains("recovered", StringComparison.Ordinal), "Later Bash call did not run.");
        }
    }

    private static async Task Failures()
    {
        List<Message> previous = [new UserMessage("keep")];
        foreach (string mode in new[] { "provider", "malformed", "text", "diagnostic" })
        {
            using var fixture = new HttpFixture();
            string body = mode switch
            {
                "provider" => HttpFixture.Events(new JsonObject { ["type"] = "error", ["message"] = "fixture failed" }),
                "malformed" => "data: {\n\n",
                "diagnostic" => CliFixture.Response("", CliFixture.Call("exit 0")),
                _ => HttpFixture.Sse("openai_text")
            };
            var response = fixture.Enqueue(HttpFixture.Wire(body));
            using var provider = ProviderChecks.Provider(false, fixture.Client);
            var failure = new IOException("consumer failed");
            TextDeltaHandler broken = (_, _) => throw failure;
            var running = TinyAgentCli.TurnAsync(provider, previous, "fail", mode == "text" ? broken : Ignore,
                mode == "diagnostic" ? broken : Ignore);
            if (mode is "text" or "diagnostic") Check.True(ReferenceEquals(failure, await Check.ThrowsAsync<IOException>(() => running)), "Consumer failure was replaced.");
            else await Check.ThrowsAsync<ProviderException>(() => running);
            Check.True(response.Closed.Task.IsCompleted, "Failed CLI turn retained HTTP I/O.");
            Check.Equal(1, previous.Count);
            Check.Equal("keep", ((UserMessage)previous[0]).Text);
        }
        var original = new IOException("original");
        using var wrapping = new ScriptedProvider(async (_, emit, token) =>
        {
            try { await emit!("text", token); } catch (Exception failure) { throw new ProviderException("wrapped", failure); }
            return new ProviderTurn("", [], [], []);
        });
        Check.True(ReferenceEquals(original, await Check.ThrowsAsync<IOException>(() => TinyAgentCli.TurnAsync(
            wrapping, previous, "fail", (_, _) => throw original, Ignore))), "Custom provider replaced consumer failure.");
    }

    private static async Task Terminal()
    {
        foreach (bool cancel in new[] { false, true })
        {
            using var fixture = new HttpFixture();
            var response = fixture.Enqueue(HttpFixture.Wire(CliFixture.Response("terminal café"), ending: false), eof: false);
            using var provider = ProviderChecks.Provider(false, fixture.Client);
            using var cancellation = new CancellationTokenSource();
            var chunks = new List<string>();
            var running = TinyAgentCli.TurnAsync(provider, [], "go", async (text, _) =>
            {
                chunks.Add(text);
                if (cancel && text == "\n") await cancellation.CancelAsync();
            }, Ignore, cancellation.Token);
            if (cancel) await Check.ThrowsAsync<OperationCanceledException>(() => running);
            else Check.Equal(2, (await running).Count);
            Check.Equal("terminal café|\n", string.Join("|", chunks));
            Check.True(response.Closed.Task.IsCompleted, "Terminal delivery retained provider I/O.");
        }
        using var source = new CancellationTokenSource();
        using var uncooperative = new ScriptedProvider(async (_, _, _) =>
        {
            await source.CancelAsync();
            return new ProviderTurn("late", [], [], []);
        });
        await Check.ThrowsAsync<OperationCanceledException>(() => TinyAgentCli.TurnAsync(uncooperative, [], "go",
            (_, _) => throw new InvalidOperationException("Delivered after cancellation."), Ignore, source.Token));
    }

    private static async Task Cancellation()
    {
        foreach (bool headers in new[] { true, false })
        {
            using var fixture = new HttpFixture();
            var response = fixture.Enqueue(headers ? [] : HttpFixture.Wire("data: {\"type\":\"response.created\"}\n\n", ending: false), eof: false);
            using var provider = ProviderChecks.Provider(false, fixture.Client);
            using var cancellation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            var running = TinyAgentCli.TurnAsync(provider, [], "wait", Ignore, Ignore, cancellation.Token);
            await response.Waiting.Task.WaitAsync(cancellation.Token);
            await cancellation.CancelAsync();
            await Check.ThrowsAsync<OperationCanceledException>(() => running);
            Check.True(response.Closed.Task.IsCompleted, "Cancelled CLI retained HTTP I/O.");
        }
        using var bashFixture = new HttpFixture();
        bashFixture.Enqueue(HttpFixture.Wire(CliFixture.Response("", CliFixture.Call("sleep 30"))));
        using var bashProvider = ProviderChecks.Provider(false, bashFixture.Client);
        using var stop = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await Check.ThrowsAsync<OperationCanceledException>(() => TinyAgentCli.TurnAsync(bashProvider, [], "bash", Ignore,
            async (_, _) => await stop.CancelAsync(), stop.Token));
        Check.Equal(1, bashFixture.Connections);
    }

    private static async Task ConsoleLoop()
    {
        var counts = new List<int>();
        using var provider = new ScriptedProvider((request, _, _) =>
        {
            counts.Add(request.History.Count);
            if (((UserMessage)request.History[^1]).Text == "fail") throw new ProviderException("expected failure");
            return Task.FromResult(new ProviderTurn("done", [], [], []));
        });
        using var input = new StringReader("\nfirst\nfail\nlast");
        using var output = new MemoryStream();
        using var error = new MemoryStream();
        Check.Equal(0, await TinyAgentCli.RunAsync(provider, input, output, error));
        Check.Equal("1,3,3", string.Join(",", counts));
        Check.True(Encoding.UTF8.GetString(error.ToArray()).Contains("error: expected failure\n", StringComparison.Ordinal), "Provider failure was not shown.");
        Check.True(!provider.Disposed && output.CanWrite && error.CanWrite, "CLI disposed caller resources.");
        Check.Equal(-1, input.Read());
        using var stopped = new CancellationTokenSource();
        await stopped.CancelAsync();
        await Check.ThrowsAsync<OperationCanceledException>(() => TinyAgentCli.RunAsync(provider, input, output, error, stopped.Token));
    }

    private static async Task NativeIdle()
    {
        await using (var cli = new CliProcess("normal", nullStdio: true))
            await cli.Exit(0);
        await using (var cli = new CliProcess("normal"))
        {
            var output = cli.Child.StandardOutput.ReadToEndAsync();
            await cli.Send("last");
            cli.Child.StandardInput.Close();
            await cli.Exit(0);
            Check.True((await output).Contains("done\ncafé\n", StringComparison.Ordinal), "Unterminated EOF prompt was lost.");
        }
        await using (var cli = new CliProcess("normal"))
        {
            Check.True((await cli.Child.StandardOutput.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(5)))!.StartsWith("TinyAgent.", StringComparison.Ordinal), "Missing CLI banner.");
            await cli.Interrupt();
            await cli.Exit(130, cleanup: false);
        }
    }

    private static async Task NativeRequest()
    {
        await using var cli = new CliProcess("headers");
        await cli.Send("wait\n");
        await cli.WaitFor(".ready");
        await cli.Interrupt();
        await cli.Exit(130);
    }

    private static async Task NativeBash()
    {
        await using var cli = new CliProcess("bash");
        await cli.Send("bash\n");
        await cli.WaitFor(".pid");
        int pid = int.Parse(await File.ReadAllTextAsync(cli.Marker + ".pid"), System.Globalization.CultureInfo.InvariantCulture);
        await cli.Interrupt();
        await cli.Exit(130);
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        while (Kill(pid, 0) == 0) await Task.Delay(10, deadline.Token);
        Check.Equal(3, Marshal.GetLastPInvokeError());
    }

    private static async Task NativeOutput()
    {
        foreach (string mode in new[] { "stdout", "stderr" })
        {
            await using var cli = new CliProcess(mode);
            await cli.Send("large\n");
            await cli.WaitFor(".ready");
            await Task.Delay(100);
            Check.True(!cli.Child.HasExited, "Output fixture exited before interrupt.");
            Check.True(!File.Exists(cli.Marker + ".delivered"), "Large output finished before interruption.");
            await cli.Interrupt();
            await cli.Exit(130);
        }
    }

    private static async Task NativeBroken()
    {
        await using var cli = new CliProcess("broken");
        await cli.Send("large\n");
        await cli.WaitFor(".ready");
        cli.Child.StandardOutput.Close();
        await cli.Exit(1);
    }

    [DllImport("libc", EntryPoint = "kill", SetLastError = true)]
    private static extern int Kill(int pid, int signal);
}
