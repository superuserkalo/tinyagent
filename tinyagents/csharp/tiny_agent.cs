using System.Text;
using System.Text.Json;
using TinyAgent;

static class TinyAgentCli
{
    const string Instructions = """
        You are TinyAgent, a coding agent in the current directory.
        Inspect before changing files. Use bash for shell and file work. Each call is a fresh process.
        """;
    static readonly Tool BashTool = new("bash",
        "Run a non-interactive bash command in the current directory.",
        JsonSerializer.Deserialize<JsonElement>("""
            {"type":"object","properties":{"command":{"type":"string","minLength":1}},
             "required":["command"],"additionalProperties":false}
            """), (arguments, token) => new ValueTask<string>(Bash.RunAsync(arguments.GetProperty("command").GetString()!, token)));

    internal static Agent CreateAgent(IProvider provider) => new(provider,
        Environment.GetEnvironmentVariable("TINYAGENT_MODEL") ?? "gpt-5.6", Instructions, [BashTool]);

    internal static async Task<IReadOnlyList<Message>> TurnAsync(IProvider provider, IReadOnlyList<Message> previous,
        string prompt, TextDeltaHandler onText, TextDeltaHandler onDiagnostic, CancellationToken cancellationToken = default)
    {
        var result = await CreateAgent(provider).RunAsync(prompt, previous, async (value, token) =>
        {
            switch (value)
            {
                case AgentEvent.TextDelta text:
                    await onText(text.Delta, token);
                    break;
                case AgentEvent.ToolExecutionStart started:
                    if (started.Call.Arguments.TryGetProperty("command", out var command) && command.ValueKind == JsonValueKind.String)
                        await onDiagnostic($"\n$ {command.GetString()}\n", token);
                    break;
                case AgentEvent.ToolExecutionEnd ended:
                    // An aborted run still pairs its tool events; console writes remain cancellable.
                    if (!token.IsCancellationRequested) await onDiagnostic(ended.Output + "\n", token);
                    break;
                case AgentEvent.Completed:
                    await onText("\n", token);
                    break;
            }
        }, cancellationToken);
        return result.History;
    }

    // The caller owns the provider and streams. Only a running turn intercepts Ctrl-C.
    internal static async Task<int> RunAsync(IProvider provider, TextReader input, Stream output, Stream error,
        CancellationToken cancellationToken = default)
    {
        IReadOnlyList<Message> history = Array.Empty<Message>();
        await WriteAsync(output, "TinyAgent. Type a task, or Ctrl-C or Ctrl-D to exit.\n", cancellationToken);
        while (true)
        {
            await WriteAsync(output, "\n> ", cancellationToken);
            string? line = await input.ReadLineAsync(cancellationToken);
            if (line is null) break;
            string prompt = line.Trim();
            if (prompt.Length == 0) continue;
            using var cancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            var gate = new object();
            bool active = true;
            bool interrupted = false;
            Task stopping = Task.CompletedTask;
            ConsoleCancelEventHandler interrupt = (_, signal) =>
            {
                lock (gate)
                {
                    if (!active) return;
                    signal.Cancel = true;
                    if (interrupted) return;
                    interrupted = true;
                    stopping = cancellation.CancelAsync();
                }
            };
            Console.CancelKeyPress += interrupt;
            try
            {
                try
                {
                    history = await TurnAsync(provider, history, prompt,
                        (text, token) => WriteAsync(output, text, token),
                        (text, token) => WriteAsync(error, text, token), cancellation.Token);
                }
                catch (OperationCanceledException) { throw; }
                catch (IOException) { throw; }
                catch (Exception failure)
                {
                    await WriteAsync(error, $"error: {failure.Message}\n", cancellation.Token);
                }
            }
            catch (OperationCanceledException) when (cancellation.IsCancellationRequested && !cancellationToken.IsCancellationRequested)
            {
                // The interrupt path returns only after callbacks and owned work finish.
            }
            finally
            {
                lock (gate) { active = false; }
                Console.CancelKeyPress -= interrupt;
                await stopping;
            }
            if (interrupted) return 130;
            cancellationToken.ThrowIfCancellationRequested();
        }
        await WriteAsync(output, "\n", cancellationToken);
        return 0;
    }

    internal static async Task<int> RunStdioAsync(IProvider provider)
    {
        bool native = OperatingSystem.IsLinux() || OperatingSystem.IsMacOS();
        await using var input = native && Console.IsInputRedirected ? Stdio.Open(0, FileAccess.Read) : null;
        using var reader = input is null ? null : new StreamReader(input, new UTF8Encoding(false, true),
            detectEncodingFromByteOrderMarks: false, leaveOpen: true);
        await using var output = native && Console.IsOutputRedirected
            ? Stdio.Open(1, FileAccess.Write) : Console.OpenStandardOutput();
        await using var error = native && Console.IsErrorRedirected
            ? Stdio.Open(2, FileAccess.Write) : Console.OpenStandardError();
        return await RunAsync(provider, reader is null ? Console.In : reader, output, error);
    }

    static async ValueTask WriteAsync(Stream stream, string text, CancellationToken token)
    {
        await stream.WriteAsync(Encoding.UTF8.GetBytes(text), token);
        await stream.FlushAsync(token);
        token.ThrowIfCancellationRequested();
    }

    static void SelfTest()
    {
        if (Bash.DeniedReason("printf 'hello\\n'") is not null) throw new Exception("safe command was denied");
        foreach (var command in new[]
        {
            "rm -rf /", "r\"\"m -rf /", "sudo echo hi", "git reset --hard HEAD",
            "find . -delete", "mkfs.ext4 /dev/sda",
        })
            if (Bash.DeniedReason(command) is null) throw new Exception($"unsafe command was allowed: {command}");
        if (BashTool.Definition.Parameters.GetProperty("required")[0].GetString() != "command")
            throw new Exception("Bash schema is missing command");
        Console.WriteLine("self-test passed");
    }

    private static async Task<int> ServeAsync(Agent agent)
    {
        var options = new HttpServerOptions
        {
            Host = Environment.GetEnvironmentVariable("TINYAGENT_HOST") ?? "127.0.0.1",
            Port = int.Parse(Environment.GetEnvironmentVariable("TINYAGENT_PORT") ?? "8080", System.Globalization.CultureInfo.InvariantCulture),
            Token = Environment.GetEnvironmentVariable("TINYAGENT_TOKEN") ?? "",
            AllowRemote = Environment.GetEnvironmentVariable("TINYAGENT_ALLOW_REMOTE") == "1"
        };
        await using var server = await HttpServer.StartAsync(agent, options);
        var stopped = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        ConsoleCancelEventHandler interrupt = (_, signal) =>
        {
            signal.Cancel = true;
            server.RequestStop();
            stopped.TrySetResult();
        };
        Console.CancelKeyPress += interrupt;
        try
        {
            Console.Error.WriteLine($"TinyAgent listening on {server.Address}");
            await stopped.Task;
        }
        finally { Console.CancelKeyPress -= interrupt; }
        return 130;
    }

    public static async Task<int> Main(string[] args)
    {
        if (args.Contains("--self-test"))
        {
            SelfTest();
            return 0;
        }
        using var provider = new OpenAIProvider();
        if (args is ["--mode", "rpc"]) return await Rpc.RunStdioAsync(CreateAgent(provider));
        if (args is ["serve"]) return await ServeAsync(CreateAgent(provider));
        if (args.Length != 0)
        {
            Console.Error.WriteLine("Usage: tinyagent [--self-test | --mode rpc | serve]");
            return 2;
        }
        try { return await RunStdioAsync(provider); }
        catch (IOException) { return 1; }
    }
}
