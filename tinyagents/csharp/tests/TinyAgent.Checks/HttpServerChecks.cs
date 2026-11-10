using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json.Nodes;

namespace TinyAgent.Checks;

internal static class HttpServerChecks
{
    internal static IEnumerable<(string Name, Func<Task> Run)> Cases =>
    [
        ("HTTP native Kestrel routes and completed session history", Routes),
        ("HTTP exposure validation and bearer authentication", Authentication),
        ("HTTP content type, JSON, UTF-8, and body limits", Validation),
        ("HTTP native framing rejects malformed and oversized chunked bodies", Framing),
        ("HTTP busy, cancellation, and failed-run rollback", BusyAndCancel),
        ("HTTP disconnect cancels and joins held provider cleanup", Disconnect),
        ("HTTP deletion stays owned through concurrent shutdown", DeleteAndShutdown),
        ("HTTP shutdown retains cleanup after transport cancellation", ShutdownCleanup),
        ("HTTP provider callback can request server shutdown", CallbackShutdown),
        ("HTTP oversized events replace the terminal and roll back", OutputLimit),
        ("HTTP final framing failure cannot commit history", FinalFramingFailure),
        ("HTTP provider cancellation releases blocked event delivery", ProviderCancellation)
    ];

    private static readonly TimeSpan Deadline = TimeSpan.FromSeconds(4);
    private static TaskCompletionSource Signal() => new(TaskCreationOptions.RunContinuationsAsynchronously);
    private static HttpContent Body(string message) => new StringContent(new JsonObject { ["message"] = message }.ToJsonString(), Encoding.UTF8, "application/json");
    private static async Task<JsonObject> Object(HttpResponseMessage response) =>
        JsonNode.Parse(await response.Content.ReadAsStringAsync())!.AsObject();
    private static async Task<string> Create(HttpClient client)
    {
        using var response = await client.PostAsync("/sessions", null);
        Check.Equal(HttpStatusCode.Created, response.StatusCode);
        return (await Object(response))["sessionId"]!.GetValue<string>();
    }
    private static async Task<List<JsonObject>> Prompt(HttpClient client, string id, string message, bool waitForIdle = false)
    {
        using var deadline = new CancellationTokenSource(Deadline);
        while (true)
        {
            using var response = await client.PostAsync($"/sessions/{id}/prompts", Body(message), deadline.Token);
            if (waitForIdle && response.StatusCode == HttpStatusCode.Conflict)
            {
                await Task.Delay(10, deadline.Token);
                continue;
            }
            Check.Equal(HttpStatusCode.OK, response.StatusCode);
            Check.Equal("application/jsonl", response.Content.Headers.ContentType!.MediaType);
            string text = await response.Content.ReadAsStringAsync(deadline.Token);
            return text.Split('\n', StringSplitOptions.RemoveEmptyEntries).Select(line => JsonNode.Parse(line)!.AsObject()).ToList();
        }
    }
    private static ScriptedProvider Echo() => new(async (request, emit, token) =>
    {
        if (emit is not null) await emit("partial\ncafé", token);
        return new ProviderTurn("history:" + request.History.Count, [], [], []);
    });

    private static async Task Routes()
    {
        using var provider = Echo();
        await using var transport = new HttpTransportFixture();
        await using var server = await HttpServer.StartAsync(new Agent(provider, "fixture"), new(), transport);
        using var client = transport.Client();
        using var health = await client.GetAsync("/health");
        Check.Equal("ok", (await Object(health))["status"]!.GetValue<string>());
        Check.True(!health.Headers.Contains("Server") && !health.Headers.Contains("Access-Control-Allow-Origin"), "Unexpected server or CORS header.");
        string id = await Create(client);
        Check.True(id.StartsWith("s_", StringComparison.Ordinal) && id.Length == 50, "Session ID lacks random bytes.");
        foreach (int count in new[] { 1, 3 })
        {
            var values = await Prompt(client, id, "go");
            Check.Equal("agent_start", values[0]["type"]!.GetValue<string>());
            Check.Equal("history:" + count, values[^1]["text"]!.GetValue<string>());
            Check.Equal("completed", values[^1]["status"]!.GetValue<string>());
            Check.Equal("partial\ncafé", values[1]["delta"]!.GetValue<string>());
            for (int i = 0; i < values.Count; i++)
            {
                Check.Equal(i + 1, values[i]["sequence"]!.GetValue<int>());
                Check.Equal(values[0]["runId"]!.GetValue<string>(), values[i]["runId"]!.GetValue<string>());
            }
        }
        string second = await Create(client);
        Check.True(id != second, "Sessions share IDs.");
        Check.Equal("history:1", (await Prompt(client, second, "other"))[^1]["text"]!.GetValue<string>());
        using var cancel = await client.PostAsync($"/sessions/{id}/cancel", null);
        Check.Equal(false, (await Object(cancel))["accepted"]!.GetValue<bool>());
        using var deleted = await client.DeleteAsync($"/sessions/{id}");
        Check.Equal(HttpStatusCode.NoContent, deleted.StatusCode);
        Check.Equal("", await deleted.Content.ReadAsStringAsync());
        using var missing = await client.PostAsync($"/sessions/{id}/prompts", Body("go"));
        Check.Equal(HttpStatusCode.NotFound, missing.StatusCode);
        Check.True(!provider.Disposed, "Server disposed its caller-owned provider.");
    }

    private static async Task Authentication()
    {
        using var provider = Echo();
        var agent = new Agent(provider, "fixture");
        foreach (var invalid in new HttpServerOptions[]
        {
            new() { Host = "0.0.0.0" }, new() { Host = "0.0.0.0", Token = "secret" },
            new() { Host = "::", AllowRemote = true }, new() { Host = "example.com" },
            new() { Token = "has space" }, new() { Token = "é" }, new() { Port = -1 }
        }) await Check.ThrowsAsync<ArgumentException>(() => HttpServer.StartAsync(agent, invalid));
        foreach (string host in new[] { "localhost", "::1", "::ffff:127.0.0.1" })
            _ = new HttpServerOptions { Host = host }.Validate();
        await using var transport = new HttpTransportFixture();
        await using var server = await HttpServer.StartAsync(agent, new() { Token = "secret" }, transport);
        using var client = transport.Client();
        using var rejected = await client.GetAsync("/health");
        Check.Equal(HttpStatusCode.Unauthorized, rejected.StatusCode);
        Check.Equal("Bearer", rejected.Headers.WwwAuthenticate.Single().Scheme);
        client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", "secret");
        using var accepted = await client.GetAsync("/health");
        Check.Equal(HttpStatusCode.OK, accepted.StatusCode);
        using var options = await client.SendAsync(new HttpRequestMessage(HttpMethod.Options, "/sessions"));
        Check.Equal(HttpStatusCode.NotFound, options.StatusCode);
        Check.True(!options.Headers.Contains("Access-Control-Allow-Origin"), "CORS was enabled.");
    }

    private static async Task Validation()
    {
        using var provider = Echo();
        await using var transport = new HttpTransportFixture();
        await using var server = await HttpServer.StartAsync(new Agent(provider, "fixture"), new(), transport);
        using var client = transport.Client();
        string id = await Create(client);
        foreach (string body in new[] { "[]", "null", "{}", "{broken}", "{\"message\":4}", "{\"message\":\"\"}", "{}{}", "{\"message\":\"\\ud800\"}" })
        {
            using var result = await client.PostAsync($"/sessions/{id}/prompts", new StringContent(body, Encoding.UTF8, "application/json"));
            Check.Equal(HttpStatusCode.BadRequest, result.StatusCode);
        }
        using var wrongType = await client.PostAsync($"/sessions/{id}/prompts", new StringContent("{\"message\":\"go\"}"));
        Check.Equal(HttpStatusCode.UnsupportedMediaType, wrongType.StatusCode);
        using var invalid = new ByteArrayContent([0xff]);
        invalid.Headers.ContentType = new MediaTypeHeaderValue("application/json");
        using var badUtf8 = await client.PostAsync($"/sessions/{id}/prompts", invalid);
        Check.Equal("invalid_utf8", (await Object(badUtf8))["error"]!["code"]!.GetValue<string>());
        using var large = await client.PostAsync("/sessions", new StringContent(new string(' ', 1_048_577), Encoding.UTF8, "application/json"));
        Check.Equal(HttpStatusCode.RequestEntityTooLarge, large.StatusCode);
    }

    private static async Task Framing()
    {
        using var provider = Echo();
        await using var transport = new HttpTransportFixture();
        await using var server = await HttpServer.StartAsync(new Agent(provider, "fixture"), new(), transport);
        foreach (var (body, status) in new (string, int)[]
        {
            ("Content-Length: 2\r\nContent-Length: 3\r\n\r\n{}", 400),
            ("Transfer-Encoding: chunked\r\n\r\ninvalid\r\n", 400),
            ("Transfer-Encoding: chunked\r\n\r\n100001\r\n" + new string(' ', 1_048_577) + "\r\n0\r\n\r\n", 413),
            ("Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n", 201)
        })
        {
            using var deadline = new CancellationTokenSource(Deadline);
            await using Stream connection = transport.Connect();
            using var reader = new StreamReader(connection, Encoding.UTF8, leaveOpen: true);
            Task<string?> received = reader.ReadLineAsync(deadline.Token).AsTask();
            await connection.WriteAsync(Encoding.UTF8.GetBytes(
                "POST /sessions HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nContent-Type: application/json\r\n" + body), deadline.Token);
            string response;
            try { response = await received ?? throw new IOException("Missing HTTP status line."); }
            catch (OperationCanceledException error) { throw new InvalidOperationException($"No response for expected HTTP {status}.", error); }
            Check.True(response.StartsWith($"HTTP/1.1 {status} ", StringComparison.Ordinal), response);
        }
    }

    private static async Task BusyAndCancel()
    {
        var entered = Signal();
        using var provider = new ScriptedProvider(async (request, _, token) =>
        {
            if (((UserMessage)request.History[^1]).Text == "wait")
            {
                entered.TrySetResult();
                await Task.Delay(Timeout.Infinite, token);
            }
            return new ProviderTurn("history:" + request.History.Count, [], [], []);
        });
        await using var transport = new HttpTransportFixture();
        await using var server = await HttpServer.StartAsync(new Agent(provider, "fixture"), new(), transport);
        using var client = transport.Client();
        string id = await Create(client);
        await Prompt(client, id, "seed");
        Task<List<JsonObject>> running = Prompt(client, id, "wait");
        await entered.Task.WaitAsync(Deadline);
        using var busy = await client.PostAsync($"/sessions/{id}/prompts", Body("busy"));
        Check.Equal(HttpStatusCode.Conflict, busy.StatusCode);
        Check.Equal(true, (await Object(busy))["error"]!["retryable"]!.GetValue<bool>());
        string second = await Create(client);
        Check.Equal("history:1", (await Prompt(client, second, "independent"))[^1]["text"]!.GetValue<string>());
        using var cancel = await client.PostAsync($"/sessions/{id}/cancel", null);
        Check.Equal(true, (await Object(cancel))["accepted"]!.GetValue<bool>());
        Check.Equal("aborted", (await running.WaitAsync(Deadline))[^1]["status"]!.GetValue<string>());
        Check.Equal("history:3", (await Prompt(client, id, "again"))[^1]["text"]!.GetValue<string>());
    }

    private static async Task Disconnect()
    {
        var entered = Signal();
        var cleanup = Signal();
        var release = Signal();
        var closed = Signal();
        using var provider = new ScriptedProvider(async (_, _, token) =>
        {
            try { entered.TrySetResult(); await Task.Delay(Timeout.Infinite, token); }
            finally { cleanup.TrySetResult(); await release.Task; closed.TrySetResult(); }
            return new ProviderTurn("", [], [], []);
        });
        await using var transport = new HttpTransportFixture();
        await using var server = await HttpServer.StartAsync(new Agent(provider, "fixture"), new(), transport);
        using var client = transport.Client();
        string id = await Create(client);
        using var request = new HttpRequestMessage(HttpMethod.Post, $"/sessions/{id}/prompts") { Content = Body("go") };
        using var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead);
        Stream stream = await response.Content.ReadAsStreamAsync();
        var buffer = new byte[8192];
        _ = await stream.ReadAsync(buffer);
        await entered.Task.WaitAsync(Deadline);
        try
        {
            response.Dispose();
            await cleanup.Task.WaitAsync(Deadline);
            Check.True(!closed.Task.IsCompleted, "Disconnect abandoned provider cleanup.");
        }
        finally { release.TrySetResult(); }
        await closed.Task.WaitAsync(Deadline);
    }

    private static async Task DeleteAndShutdown()
    {
        var entered = Signal();
        var cleanup = Signal();
        var release = Signal();
        using var provider = new ScriptedProvider(async (_, _, token) =>
        {
            try { entered.TrySetResult(); await Task.Delay(Timeout.Infinite, token); }
            finally { cleanup.TrySetResult(); await release.Task; }
            return new ProviderTurn("", [], [], []);
        });
        await using var transport = new HttpTransportFixture();
        var server = await HttpServer.StartAsync(new Agent(provider, "fixture"), new(), transport);
        using var client = transport.Client();
        string id = await Create(client);
        Task<List<JsonObject>> running = Prompt(client, id, "go");
        await entered.Task.WaitAsync(Deadline);
        Task<HttpResponseMessage> deleting = client.DeleteAsync($"/sessions/{id}");
        await cleanup.Task.WaitAsync(Deadline);
        Task stopping = server.DisposeAsync().AsTask();
        try
        {
            Check.True(!deleting.IsCompleted, "Deletion returned before provider cleanup.");
            Check.True(!stopping.IsCompleted, "Shutdown forgot a session being deleted.");
        }
        finally { release.TrySetResult(); }
        using var deleted = await deleting.WaitAsync(Deadline);
        Check.Equal(HttpStatusCode.NoContent, deleted.StatusCode);
        Check.Equal("aborted", (await running.WaitAsync(Deadline))[^1]["status"]!.GetValue<string>());
        await stopping.WaitAsync(Deadline);
        await server.DisposeAsync();
    }

    private static async Task ShutdownCleanup()
    {
        var entered = Signal();
        var cleanup = Signal();
        var release = Signal();
        using var provider = new ScriptedProvider(async (_, _, token) =>
        {
            try { entered.TrySetResult(); await Task.Delay(Timeout.Infinite, token); }
            finally { cleanup.TrySetResult(); await release.Task; }
            return new ProviderTurn("", [], [], []);
        });
        await using var transport = new HttpTransportFixture();
        await using var server = await HttpServer.StartAsync(new Agent(provider, "fixture"), new(), transport);
        using var client = transport.Client();
        string id = await Create(client);
        Task<List<JsonObject>> running = Prompt(client, id, "wait");
        await entered.Task.WaitAsync(Deadline);
        Task stopping = server.DisposeAsync().AsTask();
        try
        {
            await cleanup.Task.WaitAsync(Deadline);
            // Observe the actual transport abort, rather than assuming its timer elapsed.
            await Check.ThrowsAsync<Microsoft.AspNetCore.Connections.ConnectionAbortedException>(
                () => running.WaitAsync(Deadline));
            Check.True(!stopping.IsCompleted, "Transport cancellation abandoned provider cleanup.");
        }
        finally { release.TrySetResult(); }
        await stopping.WaitAsync(Deadline);
    }

    private static async Task CallbackShutdown()
    {
        HttpServer? server = null;
        using var provider = new ScriptedProvider((_, _, token) =>
        {
            server!.RequestStop();
            token.ThrowIfCancellationRequested();
            return Task.FromResult(new ProviderTurn("", [], [], []));
        });
        await using var transport = new HttpTransportFixture();
        server = await HttpServer.StartAsync(new Agent(provider, "fixture"), new(), transport);
        using var client = transport.Client();
        string id = await Create(client);
        var values = await Prompt(client, id, "go");
        Check.Equal("aborted", values[^1]["status"]!.GetValue<string>());
        await server.DisposeAsync().AsTask().WaitAsync(Deadline);
    }

    private static async Task OutputLimit()
    {
        using var provider = new ScriptedProvider((request, _, _) => Task.FromResult(new ProviderTurn(
            ((UserMessage)request.History[^1]).Text == "large" ? new string('é', 600_000) : "history:" + request.History.Count, [], [], [])));
        await using var transport = new HttpTransportFixture();
        await using var server = await HttpServer.StartAsync(new Agent(provider, "fixture"), new(), transport);
        using var client = transport.Client();
        string id = await Create(client);
        var values = await Prompt(client, id, "large");
        Check.Equal("output_too_large", values[^1]["error"]!["code"]!.GetValue<string>());
        Check.Equal(2, values.Count);
        Check.Equal("history:1", (await Prompt(client, id, "next"))[^1]["text"]!.GetValue<string>());
    }

    private static async Task FinalFramingFailure()
    {
        using var provider = Echo();
        await using var transport = new HttpTransportFixture();
        await using var server = await HttpServer.StartAsync(new Agent(provider, "fixture"), new(), transport);
        using var client = transport.Client();
        string id = await Create(client);
        await Prompt(client, id, "seed");
        transport.FailFinalChunk = true;
        var failure = await Check.ThrowsAsync<Microsoft.AspNetCore.Connections.ConnectionAbortedException>(() => Prompt(client, id, "failed delivery"));
        Check.Equal("Fixture final chunk failed.", failure.Message);
        transport.FailFinalChunk = false;
        // The peer sees a failed write before the run has necessarily joined its cleanup.
        Check.Equal("history:3", (await Prompt(client, id, "next", waitForIdle: true))[^1]["text"]!.GetValue<string>());
    }

    private static async Task ProviderCancellation()
    {
        var entered = Signal();
        var cleaned = Signal();
        using var deadline = new CancellationTokenSource();
        using var provider = new ScriptedProvider(async (_, emit, _) =>
        {
            try
            {
                entered.TrySetResult();
                await emit!(new string('x', 500_000), deadline.Token);
                return new ProviderTurn("done", [], [], []);
            }
            finally { cleaned.TrySetResult(); }
        });
        await using var transport = new HttpTransportFixture();
        await using var server = await HttpServer.StartAsync(new Agent(provider, "fixture"), new(), transport);
        using var client = transport.Client();
        string id = await Create(client);
        using var request = new HttpRequestMessage(HttpMethod.Post, $"/sessions/{id}/prompts") { Content = Body("go") };
        using var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead);
        _ = await (await response.Content.ReadAsStreamAsync()).ReadAsync(new byte[8192]);
        await entered.Task.WaitAsync(Deadline);
        // Only the provider callback token is cancelled; the client keeps its response open.
        await deadline.CancelAsync();
        await cleaned.Task.WaitAsync(Deadline);
    }

    internal static async Task Tcp()
    {
        var entered = Signal();
        var cleanupStarted = Signal();
        var release = Signal();
        using var provider = new ScriptedProvider(async (request, _, token) =>
        {
            if (((UserMessage)request.History[^1]).Text == "wait")
            {
                try { entered.TrySetResult(); await Task.Delay(Timeout.Infinite, token); }
                finally { cleanupStarted.TrySetResult(); await release.Task; }
            }
            return new ProviderTurn("history:" + request.History.Count, [], [], []);
        });
        await using var server = await HttpServer.StartAsync(new Agent(provider, "fixture"), new() { Port = 0 });
        using var client = new HttpClient(new SocketsHttpHandler { UseProxy = false, UseCookies = false, MaxResponseDrainSize = 0 })
        { BaseAddress = server.Address, Timeout = TimeSpan.FromSeconds(5) };
        try
        {
            string id = await Create(client);
            Check.Equal("history:1", (await Prompt(client, id, "seed"))[^1]["text"]!.GetValue<string>());
            using var request = new HttpRequestMessage(HttpMethod.Post, $"/sessions/{id}/prompts") { Content = Body("wait") };
            using var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead);
            _ = await (await response.Content.ReadAsStreamAsync()).ReadAsync(new byte[8192]);
            await entered.Task.WaitAsync(Deadline);
            response.Dispose();
            await cleanupStarted.Task.WaitAsync(Deadline);
            using var busy = await client.PostAsync($"/sessions/{id}/prompts", Body("before cleanup"));
            Check.Equal(HttpStatusCode.Conflict, busy.StatusCode);
            Check.Equal("session_busy", (await Object(busy))["error"]!["code"]!.GetValue<string>());
            release.TrySetResult();
            Check.Equal("history:3", (await Prompt(client, id, "next", waitForIdle: true))[^1]["text"]!.GetValue<string>());
        }
        finally { release.TrySetResult(); }
    }
}
