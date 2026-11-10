using System.Diagnostics;
using System.Text;
using System.Text.Json.Nodes;

namespace TinyAgent.Checks;

internal static class RpcChecks
{
    internal static IEnumerable<(string Name, Func<Task> Run)> Cases =>
    [
        ("RPC native subprocess shared command fixtures", SharedFixtures),
        ("RPC native redirected files", RedirectedFiles),
        ("RPC native completion, framing, and history rollback", Completion),
        ("RPC native busy, abort, and reset", BusyAbortReset),
        ("RPC native strict JSON and final partial input", JsonInput),
        ("RPC native UTF-8 and input byte limits", InputBounds),
        ("RPC native oversized output cancellation and reuse", OutputBounds),
        ("RPC native EOF and Ctrl-C cleanup", Shutdown),
        ("RPC native stalled acknowledgement releases I/O", StalledOutput),
        ("RPC native cancelled output drains and permits reuse", DrainedOutput),
        ("RPC native queue overflow preserves complete frames", QueueOverflow),
        ("RPC native broken output cancels the provider", BrokenOutput),
        ("RPC owned streams, flush failure, and caller cancellation", StreamOwnership)
    ];

    private static JsonObject Command(string type, string id = "id") => new() { ["type"] = type, ["id"] = id };
    private static JsonObject Prompt(string message, string id = "prompt") => new() { ["type"] = "prompt", ["message"] = message, ["id"] = id };
    private static string Type(JsonObject value) => value["type"]!.GetValue<string>();
    private static string Code(JsonObject value) => value["error"]!["code"]!.GetValue<string>();
    private static JsonObject Fixtures() => JsonNode.Parse(File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "fixtures/rpc_v1.json")))!.AsObject();

    private static async Task<List<JsonObject>> Run(RpcProcess process, string prompt)
    {
        await process.Send(Prompt(prompt));
        var response = await process.Read();
        Check.Equal("response", Type(response));
        Check.True(response["success"]!.GetValue<bool>(), "Prompt was rejected.");
        string id = response["data"]!["runId"]!.GetValue<string>();
        List<JsonObject> events = [];
        while (true)
        {
            var value = await process.Read();
            Check.Equal(id, value["runId"]!.GetValue<string>());
            Check.Equal((long)events.Count + 1, value["sequence"]!.GetValue<long>());
            events.Add(value);
            if (Type(value) == "agent_end") return events;
        }
    }

    private static async Task Idle(RpcProcess process, int count)
    {
        for (int attempt = 0; attempt < 100; attempt++)
        {
            await process.Send(Command("get_state"));
            var response = await process.Read();
            Check.Equal("response", Type(response));
            var data = response["data"]!;
            if (data["status"]!.GetValue<string>() == "idle")
            {
                Check.Equal(count, data["messageCount"]!.GetValue<int>());
                Check.True(data["runId"] is null, "Idle state retained a run ID.");
                return;
            }
            await Task.Delay(10);
        }
        throw new TimeoutException("RPC did not become idle.");
    }

    private static async Task StartWaiting(RpcProcess process)
    {
        await process.Send(Prompt("wait"));
        Check.Equal("response", Type(await process.Read()));
        Check.Equal("agent_start", Type(await process.Read()));
        var delta = await process.Read();
        Check.Equal("text_delta", Type(delta));
        Check.Equal("waiting", delta["delta"]!.GetValue<string>());
    }

    private static async Task SharedFixtures()
    {
        await using var process = new RpcProcess();
        foreach (var scenario in Fixtures()["scenarios"]!.AsArray().Where(value => value?["expected"] is not null))
        {
            foreach (var command in scenario!["commands"]!.AsArray()) await process.Send(command!.AsObject());
            foreach (var expected in scenario["expected"]!.AsArray())
                Check.True(JsonNode.DeepEquals(expected, await process.Read()), $"RPC shared fixture failed: {scenario["name"]}");
        }
        process.Eof();
        Check.Equal("settled:0:0\n", await process.Exit(0));
        Check.Equal("", await process.Child.StandardOutput.ReadToEndAsync());
    }

    private static async Task RedirectedFiles()
    {
        string directory = Path.Combine(AppContext.BaseDirectory, $"rpc-files-{Guid.NewGuid():N}");
        Directory.CreateDirectory(directory);
        try
        {
            string input = Path.Combine(directory, "input.jsonl");
            string output = Path.Combine(directory, "output.jsonl");
            var scenario = Fixtures()["scenarios"]![0]!;
            await File.WriteAllTextAsync(input, scenario["commands"]![0]!.ToJsonString() + "\n");
            var info = new ProcessStartInfo("/bin/sh")
            {
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true
            };
            info.ArgumentList.Add("-c");
            info.ArgumentList.Add("exec \"$@\" < \"$TA_RPC_INPUT\" > \"$TA_RPC_OUTPUT\"");
            info.ArgumentList.Add("tinyagent-rpc-files");
            info.ArgumentList.Add(Environment.ProcessPath ?? throw new InvalidOperationException("Missing dotnet executable."));
            info.ArgumentList.Add(typeof(Program).Assembly.Location);
            info.ArgumentList.Add("--rpc-fixture");
            info.Environment["TA_RPC_INPUT"] = input;
            info.Environment["TA_RPC_OUTPUT"] = output;
            info.Environment.Remove("OPENAI_API_KEY");
            info.Environment.Remove("ANTHROPIC_API_KEY");
            using var child = Process.Start(info) ?? throw new InvalidOperationException("File RPC child did not start.");
            try
            {
                await child.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(4));
                Check.Equal("settled:0:0\n", await child.StandardError.ReadToEndAsync());
                Check.Equal(0, child.ExitCode);
                var actual = JsonNode.Parse(await File.ReadAllTextAsync(output));
                Check.True(JsonNode.DeepEquals(scenario["expected"]![0], actual), "Redirected file RPC response changed.");
                Check.Equal("", await child.StandardOutput.ReadToEndAsync());
            }
            finally
            {
                if (!child.HasExited) { child.Kill(); await child.WaitForExitAsync(); }
            }
        }
        finally { Directory.Delete(directory, recursive: true); }
    }

    private static async Task Completion()
    {
        await using var process = new RpcProcess();
        var events = await Run(process, "seed");
        Check.Equal("agent_start", Type(events[0]));
        Check.Equal("completed", events[^1]["status"]!.GetValue<string>());
        Check.Equal("done\ncafé", events[^1]["text"]!.GetValue<string>());
        Check.Equal("done\ncafé", string.Concat(events.Where(value => Type(value) == "text_delta").Select(value => value["delta"]!.GetValue<string>())));
        await Idle(process, 2);
        events = await Run(process, "fail");
        Check.Equal("failed", events[^1]["status"]!.GetValue<string>());
        Check.Equal("provider_error", Code(events[^1]));
        await Idle(process, 2);
        events = await Run(process, "history");
        Check.Equal("seed,history", events[^1]["text"]!.GetValue<string>());
        await Idle(process, 4);
        process.Eof();
        Check.Equal("settled:3:0\n", await process.Exit(0));
    }

    private static async Task BusyAbortReset()
    {
        await using var process = new RpcProcess();
        await Run(process, "seed");
        await Idle(process, 2);
        await StartWaiting(process);
        await process.Send(Prompt("busy", "busy"));
        Check.Equal("busy", Code(await process.Read()));
        await process.Send(Command("abort"));
        bool response = false;
        bool terminal = false;
        for (int index = 0; index < 2; index++)
        {
            var value = await process.Read();
            if (Type(value) == "response") response = value["data"]!["accepted"]!.GetValue<bool>();
            else terminal = value["status"]!.GetValue<string>() == "aborted";
        }
        Check.True(response && terminal, "Abort omitted its acknowledgement or terminal event.");
        await Idle(process, 2);
        await StartWaiting(process);
        await process.Send(Command("new_session"));
        Check.Equal("aborted", (await process.Read())["status"]!.GetValue<string>());
        Check.True((await process.Read())["data"]!["abortedRun"]!.GetValue<bool>(), "Reset missed an active run.");
        await Idle(process, 0);
        var history = await Run(process, "history");
        Check.Equal("history", history[^1]["text"]!.GetValue<string>());
        process.Eof();
        await process.Exit(0);
    }

    private static async Task JsonInput()
    {
        await using var process = new RpcProcess();
        foreach (string line in new[] { "{", "", "{\"id\":NaN}", "{\"id\":Infinity}", "{\"id\":1,}", "{\"type\":\"get_state\",\"extra\":\"\\ud800\"}", "{\"id\":\"first\",\"id\":\"second\"}" })
        {
            await process.Raw(Encoding.UTF8.GetBytes(line + "\n"));
            Check.Equal("invalid_json", Code(await process.Read()));
        }
        await process.Raw("[]\n"u8.ToArray());
        Check.Equal("invalid_command", Code(await process.Read()));
        await process.Raw("{\"id\":\"final\",\"type\":\"get_state\"}"u8.ToArray());
        process.Eof();
        Check.Equal("final", (await process.Read())["id"]!.GetValue<string>());
        await process.Exit(0);
    }

    private static async Task InputBounds()
    {
        int maximum = Fixtures()["limits"]!["jsonlLineBytes"]!.GetValue<int>();
        foreach (bool oversized in new[] { false, true })
        {
            await using var process = new RpcProcess();
            string prefix = "{\"id\":\"limit\",\"type\":\"get_state\",\"padding\":\"";
            string source = prefix + new string('x', maximum - prefix.Length - 2 + (oversized ? 1 : 0)) + "\"}\n";
            try { await process.Raw(Encoding.UTF8.GetBytes(source)); }
            catch (IOException) when (oversized) { }
            var response = await process.Read();
            if (oversized) Check.Equal("line_too_large", Code(response));
            else
            {
                Check.Equal("limit", response["id"]!.GetValue<string>());
                await process.Send(Command("get_state", "after"));
                Check.Equal("after", (await process.Read())["id"]!.GetValue<string>());
                process.Eof();
            }
            await process.Exit(0);
        }
        foreach (byte[] invalid in new byte[][] { [0xff, 10], [0xe2, 0x82, 10], [0xc0, 0xaf, 10] })
        {
            await using var process = new RpcProcess();
            await process.Raw(invalid);
            Check.Equal("invalid_utf8", Code(await process.Read()));
            await process.Exit(0);
        }
    }

    private static async Task OutputBounds()
    {
        await using var process = new RpcProcess();
        await process.Send(Prompt("large"));
        Check.Equal("response", Type(await process.Read()));
        Check.Equal("agent_start", Type(await process.Read()));
        Check.Equal("output_too_large", Code(await process.Read()));
        await Idle(process, 0);
        await Run(process, "next");
        await Idle(process, 2);
        await StartWaiting(process);
        var oversized = Command("get_state", "");
        int maximum = Fixtures()["limits"]!["jsonlLineBytes"]!.GetValue<int>();
        oversized["id"] = new string('x', maximum - Encoding.UTF8.GetByteCount(oversized.ToJsonString()));
        await process.Send(oversized);
        Check.Equal("output_too_large", Code(await process.Read()));
        Check.Equal("aborted", (await process.Read())["status"]!.GetValue<string>());
        await Idle(process, 2);
        process.Eof();
        await process.Exit(0);
    }

    private static async Task Shutdown()
    {
        foreach (bool signal in new[] { false, true })
        {
            await using var process = new RpcProcess();
            await StartWaiting(process);
            if (signal) await process.Interrupt();
            else process.Eof();
            Check.Equal("aborted", (await process.Read())["status"]!.GetValue<string>());
            Check.Equal("provider_cancelled\nsettled:1:0\n", await process.Exit(signal ? 130 : 0));
        }
    }

    private static async Task StalledOutput()
    {
        await using var process = new RpcProcess();
        await process.Send(Prompt("wait", new string('x', 700_000)));
        await process.WaitForOutput();
        process.Eof();
        string errors = await process.Exit(1);
        Check.Equal("rpc:output_stalled\nsettled:0:0\n", errors);
    }

    private static async Task DrainedOutput()
    {
        await using var process = new RpcProcess();
        string id = new('x', 700_000);
        await process.Send(Prompt("wait", id));
        await process.WaitForOutput();
        await process.Send(Command("abort"));
        Check.True(id == (await process.Read())["id"]!.GetValue<string>(), "Prompt acknowledgement ID changed.");
        bool abort = false;
        bool terminal = false;
        while (!abort || !terminal)
        {
            var value = await process.Read();
            if (Type(value) == "response") abort = value["data"]!["accepted"]!.GetValue<bool>();
            else if (Type(value) == "agent_end") terminal = value["status"]!.GetValue<string>() == "aborted";
        }
        await Idle(process, 0);
        await Run(process, "next");
        await Idle(process, 2);
        process.Eof();
        await process.Exit(0);
    }

    private static async Task QueueOverflow()
    {
        await using var process = new RpcProcess();
        await StartWaiting(process);
        string id = new('x', 750_000);
        byte[] command = Encoding.UTF8.GetBytes(Command("get_state", id).ToJsonString() + "\n");
        await process.Raw(command);
        await process.WaitForOutput();
        for (int index = 0; index < 5; index++) await process.Raw(command);
        Check.Equal("provider_cancelled", await process.Child.StandardError.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(4)));
        var first = await process.Read();
        Check.Equal("response", Type(first));
        Check.True(id == first["id"]!.GetValue<string>(), "Queued response ID changed.");
        Check.Equal("output_queue_full", Code(await process.Read()));
        Check.Equal("rpc:output_queue_full\nsettled:1:0\n", await process.Exit(1));
        Check.Equal("", await process.Child.StandardOutput.ReadToEndAsync());
    }

    private static async Task BrokenOutput()
    {
        await using var process = new RpcProcess();
        await process.Send(Prompt("chunk"));
        Check.Equal("response", Type(await process.Read()));
        Check.Equal("agent_start", Type(await process.Read()));
        await process.WaitForOutput();
        process.Child.StandardOutput.Close();
        string errors = await process.Exit(1);
        Check.True(errors.StartsWith("rpc:", StringComparison.Ordinal), "Broken output did not report a transport error.");
        Check.True(errors.EndsWith("settled:1:0\n", StringComparison.Ordinal), "Broken output retained its provider.");
    }

    private static async Task StreamOwnership()
    {
        using var provider = new ScriptedProvider((_, _, _) => throw new InvalidOperationException("Idle commands reached provider."));
        var agent = new Agent(provider, "fixture");
        foreach (bool flushFails in new[] { false, true })
        {
            var input = new RpcInput("{\"type\":\"get_state\"}\n"u8.ToArray());
            var output = new RpcWriter(flushFails, input.Waiting.Task);
            using var cancelled = new CancellationTokenSource();
            var task = Rpc.RunAsync(agent, input, output, cancelled.Token);
            await input.Waiting.Task.WaitAsync(TimeSpan.FromSeconds(3));
            if (flushFails)
            {
                var error = await Check.ThrowsAsync<IOException>(() => task.WaitAsync(TimeSpan.FromSeconds(3)));
                Check.Equal("flush failed", error.Message);
            }
            else
            {
                cancelled.Cancel();
                await Check.ThrowsAsync<OperationCanceledException>(() => task.WaitAsync(TimeSpan.FromSeconds(3)));
            }
            Check.True(input.Disposed && input.Settled && output.Disposed, "RPC returned before owned I/O cleanup.");
            Check.True(!provider.Disposed, "RPC disposed the caller's provider.");
        }
    }
}

internal sealed class RpcInput(byte[] bytes) : MemoryStream(bytes)
{
    internal TaskCompletionSource Waiting { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
    internal bool Settled { get; private set; }
    internal bool Disposed { get; private set; }

    public override async ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default)
    {
        int size = await base.ReadAsync(buffer, cancellationToken);
        if (size != 0) return size;
        Waiting.TrySetResult();
        try { await Task.Delay(Timeout.Infinite, cancellationToken); }
        finally { Settled = true; }
        return 0;
    }

    protected override void Dispose(bool disposing)
    {
        Disposed = true;
        base.Dispose(disposing);
    }
}

internal sealed class RpcWriter(bool flushFails, Task inputWaiting) : MemoryStream
{
    internal bool Disposed { get; private set; }

    public override async Task FlushAsync(CancellationToken cancellationToken)
    {
        await inputWaiting.WaitAsync(cancellationToken);
        if (flushFails) throw new IOException("flush failed");
    }

    protected override void Dispose(bool disposing)
    {
        Disposed = true;
        base.Dispose(disposing);
    }
}
