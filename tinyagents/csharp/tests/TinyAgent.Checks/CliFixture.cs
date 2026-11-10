using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace TinyAgent.Checks;

internal static class CliFixture
{
    internal static JsonObject Call(string command, string id = "c") => new()
    {
        ["type"] = "function_call",
        ["call_id"] = id,
        ["name"] = "bash",
        ["arguments"] = new JsonObject { ["command"] = command }.ToJsonString()
    };

    internal static string Response(string text, params JsonObject[] items) => HttpFixture.Events(new JsonObject
    {
        ["type"] = "response.completed",
        ["response"] = new JsonObject
        {
            ["output"] = new JsonArray(items.Concat(text.Length == 0 ? [] : new[]
            {
                new JsonObject
                {
                    ["type"] = "message", ["content"] = new JsonArray(new JsonObject
                    { ["type"] = "output_text", ["text"] = text })
                }
            }).Select(value => (JsonNode)value).ToArray())
        }
    });

    internal static string Quote(string value) => "'" + value.Replace("'", "'\\''", StringComparison.Ordinal) + "'";

    internal static async Task<int> RunAsync(string mode, string marker)
    {
        int active = 0;
        int calls = 0;
        bool closed = true;
        var provider = new ScriptedProvider(async (request, emit, token) =>
        {
            active++;
            calls++;
            using var fixture = new HttpFixture();
            string body = mode switch
            {
                "headers" => "",
                "bash" => Response("", Call($"sleep 30 & child=$!; printf '%s' \"$child\" > {Quote(marker + ".pid")}; wait")),
                "stdout" or "broken" => HttpFixture.Events(new JsonObject
                { ["type"] = "response.output_text.delta", ["delta"] = new string('x', 524288) }) + Response("done"),
                "stderr" => Response("", Call("printf '" + new string('x', 131072) + "'")),
                _ => HttpFixture.Sse("openai_text")
            };
            var response = fixture.Enqueue(mode == "headers" ? [] : HttpFixture.Wire(body, ending: false),
                eof: false, fragment: 8192);
            using var native = ProviderChecks.Provider(false, fixture.Client);
            try
            {
                var running = native.StreamAsync(request, async (text, deliveryToken) =>
                {
                    await File.WriteAllTextAsync(marker + ".ready", "ready", deliveryToken);
                    if (emit is not null) await emit(text, deliveryToken);
                    if (mode is "stdout" or "broken")
                        await File.WriteAllTextAsync(marker + ".delivered", "done", deliveryToken);
                }, token);
                if (mode == "headers")
                {
                    await response.Waiting.Task.WaitAsync(token);
                    await File.WriteAllTextAsync(marker + ".ready", "ready", token);
                }
                else if (mode == "stderr")
                    await File.WriteAllTextAsync(marker + ".ready", "ready", token);
                return await running;
            }
            finally
            {
                active--;
                closed &= response.Closed.Task.IsCompleted;
            }
        });
        try
        {
            using (provider) return await TinyAgentCli.RunStdioAsync(provider);
        }
        catch (IOException) { return 1; }
        finally
        {
            await File.WriteAllTextAsync(marker + ".cleanup", JsonSerializer.Serialize(new
            { active, calls, closed, disposed = provider.Disposed }));
        }
    }
}

internal sealed class CliProcess : IAsyncDisposable
{
    internal Process Child { get; }
    internal string Marker { get; } = Path.Combine(AppContext.BaseDirectory, "cli-" + Guid.NewGuid().ToString("N"));

    internal CliProcess(string mode, bool nullStdio = false)
    {
        string dotnet = Environment.ProcessPath ?? throw new InvalidOperationException("Missing dotnet executable.");
        var info = new ProcessStartInfo(nullStdio ? "/bin/bash" : dotnet)
        {
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            WorkingDirectory = AppContext.BaseDirectory
        };
        if (nullStdio)
            foreach (var argument in new[] { "--noprofile", "--norc", "-c", "exec \"$@\" </dev/null >/dev/null 2>/dev/null", "tinyagent-check", dotnet })
                info.ArgumentList.Add(argument);
        foreach (var argument in new[] { typeof(Program).Assembly.Location, "--cli-fixture", mode, Marker })
            info.ArgumentList.Add(argument);
        info.Environment.Remove("OPENAI_API_KEY");
        info.Environment.Remove("ANTHROPIC_API_KEY");
        info.Environment.Remove("BASH_ENV");
        info.Environment.Remove("ENV");
        Child = Process.Start(info) ?? throw new InvalidOperationException("CLI child did not start.");
    }

    internal async Task Send(string text)
    {
        await Child.StandardInput.WriteAsync(text).WaitAsync(TimeSpan.FromSeconds(5));
        await Child.StandardInput.FlushAsync().WaitAsync(TimeSpan.FromSeconds(5));
    }

    internal async Task WaitFor(string suffix)
    {
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        while (!File.Exists(Marker + suffix)) await Task.Delay(10, deadline.Token);
    }

    internal async Task Interrupt()
    {
        var info = new ProcessStartInfo("/bin/kill") { UseShellExecute = false };
        info.ArgumentList.Add("-INT");
        info.ArgumentList.Add(Child.Id.ToString(System.Globalization.CultureInfo.InvariantCulture));
        using var signal = Process.Start(info) ?? throw new InvalidOperationException("Signal process did not start.");
        await signal.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5));
        Check.Equal(0, signal.ExitCode);
    }

    internal async Task Exit(int expected, bool cleanup = true)
    {
        await Child.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5));
        string error = await Child.StandardError.ReadToEndAsync();
        Check.True(Child.ExitCode == expected, $"CLI exited {Child.ExitCode}, expected {expected}: {error}");
        if (!cleanup) return;
        using var record = JsonDocument.Parse(await File.ReadAllTextAsync(Marker + ".cleanup"));
        Check.Equal(0, record.RootElement.GetProperty("active").GetInt32());
        Check.True(record.RootElement.GetProperty("closed").GetBoolean(), "CLI retained HTTP I/O.");
        Check.True(record.RootElement.GetProperty("disposed").GetBoolean(), "CLI fixture retained its provider.");
    }

    public async ValueTask DisposeAsync()
    {
        try
        {
            if (!Child.HasExited)
            {
                Child.Kill();
                await Child.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5));
            }
        }
        finally
        {
            Child.Dispose();
            foreach (string suffix in new[] { ".ready", ".pid", ".cleanup", ".delivered" }) File.Delete(Marker + suffix);
        }
    }
}
