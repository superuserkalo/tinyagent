using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace TinyAgent.Checks;

internal static class RpcFixture
{
    internal static async Task<int> RunAsync()
    {
        int calls = 0;
        int active = 0;
        using var provider = new ScriptedProvider(async (request, emit, token) =>
        {
            calls++;
            Interlocked.Increment(ref active);
            try
            {
                string prompt = ((UserMessage)request.History[^1]).Text;
                if (prompt is "large" or "chunk")
                {
                    if (emit is not null) await emit(new string('x', prompt == "large" ? 1_048_576 : 262_144), token);
                    return new ProviderTurn(prompt, [], [], []);
                }
                if (prompt == "fail")
                {
                    if (emit is not null) await emit("partial", token);
                    throw new ProviderException("fixture provider failed");
                }
                if (prompt == "history")
                    return new ProviderTurn(string.Join(",", request.History.OfType<UserMessage>().Select(value => value.Text)), [], [], []);
                using var fixture = new HttpFixture();
                string sse = prompt == "wait"
                    ? "data: {\"type\":\"response.output_text.delta\",\"delta\":\"waiting\"}\n\n"
                    : HttpFixture.Sse("openai_text");
                var response = fixture.Enqueue(HttpFixture.Wire(sse, ending: prompt != "wait"), eof: prompt != "wait");
                using var native = ProviderChecks.Provider(false, fixture.Client);
                try { return await native.StreamAsync(request, emit, token); }
                finally
                {
                    Check.True(response.Closed.Task.IsCompleted, "RPC retained native provider I/O.");
                    if (prompt == "wait" && token.IsCancellationRequested)
                        Console.Error.WriteLine("provider_cancelled");
                }
            }
            finally { Interlocked.Decrement(ref active); }
        });
        try
        {
            return await Rpc.RunStdioAsync(new Agent(provider, "fixture"));
        }
        catch (RpcException error)
        {
            Console.Error.WriteLine($"rpc:{error.Code}");
            return 1;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine($"rpc:{error.GetType().Name}:{error.Message}");
            return 1;
        }
        finally
        {
            Console.Error.WriteLine($"settled:{calls}:{active}");
        }
    }
}

internal sealed class RpcProcess : IAsyncDisposable
{
    internal Process Child { get; }
    private string prefix = "";

    internal RpcProcess()
    {
        var info = new ProcessStartInfo(Environment.ProcessPath ?? throw new InvalidOperationException("Missing dotnet executable."))
        {
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            WorkingDirectory = AppContext.BaseDirectory
        };
        info.ArgumentList.Add(typeof(Program).Assembly.Location);
        info.ArgumentList.Add("--rpc-fixture");
        info.Environment.Remove("OPENAI_API_KEY");
        info.Environment.Remove("ANTHROPIC_API_KEY");
        Child = Process.Start(info) ?? throw new InvalidOperationException("RPC child did not start.");
    }

    internal Task Send(JsonObject command) => Raw(Encoding.UTF8.GetBytes(command.ToJsonString() + "\n"));

    internal async Task Raw(byte[] bytes)
    {
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(4));
        await Child.StandardInput.BaseStream.WriteAsync(bytes, timeout.Token);
        await Child.StandardInput.BaseStream.FlushAsync(timeout.Token);
    }

    internal async Task<JsonObject> Read()
    {
        string? line = await Child.StandardOutput.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(4));
        if (line is null) throw new IOException("RPC child output ended before the expected frame.");
        line = prefix + line;
        prefix = "";
        return JsonNode.Parse(line)?.AsObject() ?? throw new IOException("RPC output was not an object.");
    }

    internal async Task WaitForOutput()
    {
        char[] first = new char[1];
        int count = await Child.StandardOutput.ReadAsync(first.AsMemory()).AsTask().WaitAsync(TimeSpan.FromSeconds(4));
        Check.Equal(1, count);
        prefix = new string(first);
    }

    internal void Eof() => Child.StandardInput.Close();

    internal async Task<string> Exit(int expected)
    {
        await Child.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(4));
        string error = await Child.StandardError.ReadToEndAsync();
        Check.True(Child.ExitCode == expected, $"RPC child exited {Child.ExitCode}, expected {expected}: {error}");
        Check.True(error.TrimEnd().EndsWith(":0", StringComparison.Ordinal), $"RPC child returned with active provider work: {error}");
        return error;
    }

    internal async Task Interrupt()
    {
        var info = new ProcessStartInfo("/bin/kill") { UseShellExecute = false };
        info.ArgumentList.Add("-INT");
        info.ArgumentList.Add(Child.Id.ToString(System.Globalization.CultureInfo.InvariantCulture));
        using var signal = Process.Start(info) ?? throw new InvalidOperationException("Signal process did not start.");
        await signal.WaitForExitAsync();
        Check.Equal(0, signal.ExitCode);
    }

    public async ValueTask DisposeAsync()
    {
        try
        {
            if (!Child.HasExited)
            {
                Child.Kill();
                await Child.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(3));
            }
        }
        finally { Child.Dispose(); }
    }
}
