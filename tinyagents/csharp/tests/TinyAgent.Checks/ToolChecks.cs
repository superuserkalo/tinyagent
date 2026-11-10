using System.Text.Json;
using System.Text.Json.Nodes;

namespace TinyAgent.Checks;

internal static class ToolChecks
{
    internal static IEnumerable<(string Name, Func<Task> Run)> Cases =>
    [
        ("Tools shared Draft 2020-12 schemas and immutable arguments", Schemas),
        ("Tools reject invalid schemas, unresolved references, and cycles", InvalidSchemas),
        ("Tools replay sequential results through both native providers", NativeLoop),
        ("Tools recover from argument, handler, Unicode, and size failures", Recovery),
        ("Tools cancellation pairs events and awaits cleanup", Cancellation),
        ("Tools callback failures stop execution and preserve history", ConsumerFailures),
        ("Tools share RPC and HTTP sessions", () => Transports())
    ];

    private static JsonElement Parse(string json) => JsonSerializer.Deserialize<JsonElement>(json);
    private static Tool Echo(string schema = "{}") => new("echo", "Echo text", Parse(schema),
        (arguments, _) => ValueTask.FromResult(arguments.GetRawText()));
    private static ProviderTurn Calls(params ToolCall[] calls) => new("", calls, [], []);
    private static ProviderTurn Done() => new("done", [], [], []);

    private static async Task Schemas()
    {
        using var fixture = JsonDocument.Parse(File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "fixtures/tool_arguments.json")));
        foreach (var item in fixture.RootElement.GetProperty("cases").EnumerateArray())
        {
            int executions = 0;
            var tool = new Tool("test", "Fixture", item.GetProperty("schema"), (arguments, _) =>
            {
                executions++;
                return ValueTask.FromResult(arguments.GetRawText());
            });
            foreach (var valid in item.GetProperty("valid").EnumerateArray())
                Check.Equal(valid.GetRawText(), await tool.ExecuteAsync(valid, default));
            int before = executions;
            foreach (var invalid in item.GetProperty("invalid").EnumerateArray())
                await Check.ThrowsAsync<ArgumentException>(() => tool.ExecuteAsync(invalid, default).AsTask());
            Check.Equal(before, executions);
        }
        var annotated = Echo("""{"properties":{"value":{"type":"string","format":"email"},"missing":{"default":2}}}""");
        Check.Equal("{\"value\":\"not an email\"}", await annotated.ExecuteAsync(Parse("""{"value":"not an email"}"""), default));
        using var document = JsonDocument.Parse("""{"type":"object"}""");
        var copy = new Tool("copy", "", document.RootElement, (_, _) => ValueTask.FromResult("ok"));
        document.Dispose();
        Check.Equal("ok", await copy.ExecuteAsync(Parse("{}"), default));
        using var provider = new ScriptedProvider((_, _, _) => Task.FromResult(Done()));
        await Check.ThrowsAsync<ArgumentException>(() => Task.FromResult(new Agent(provider, "fixture", tools: [copy, copy])));
    }

    private static async Task InvalidSchemas()
    {
        foreach (string schema in new[]
        {
            "7", """{"type":"wrong"}""", """{"required":["x","x"]}""", """{"minLength":-1}""",
            """{"pattern":"["}""", """{"$ref":"#/$defs/missing"}""", """{"$ref":"https://example.invalid/schema"}""",
            """{"properties":{"x":{"$ref":"https://example.invalid/nested"}}}""",
            """{"$schema":"http://json-schema.org/draft-07/schema#"}""", """{"not":{"$ref":"#"}}""",
            """{"properties":{"x":{"allOf":[{"$ref":"#/properties/x"}]}}}"""
        })
        {
            bool rejected = false;
            try { Echo(schema); }
            catch (Exception) { rejected = true; }
            Check.True(rejected, "Invalid schema accepted: " + schema);
        }
        await Check.ThrowsAsync<ArgumentException>(() => Echo("false").ExecuteAsync(Parse("{}"), default).AsTask());
        Check.Equal("{}", await Echo("true").ExecuteAsync(Parse("{}"), default));
    }

    private static async Task NativeLoop()
    {
        foreach (bool anthropic in new[] { false, true })
        {
            using var fixture = new HttpFixture();
            fixture.Enqueue(HttpFixture.Wire(HttpFixture.Sse(anthropic ? "anthropic_tool" : "openai_tool")));
            var next = fixture.Enqueue(HttpFixture.Wire(HttpFixture.Sse(anthropic ? "anthropic_text" : "openai_text")));
            using var provider = ProviderChecks.Provider(anthropic, fixture.Client);
            int calls = 0;
            var echo = new Tool("echo", "Fixture", Parse("{}"), (_, _) => { calls++; return ValueTask.FromResult("observation"); });
            List<AgentEvent> events = [];
            var result = await new Agent(provider, "fixture", tools: [echo]).RunAsync("go", onEvent: (value, _) =>
            { events.Add(value); return ValueTask.CompletedTask; });
            Check.Equal(1, calls);
            Check.Equal(4, result.History.Count);
            Check.Equal("observation", ((ToolResultMessage)result.History[2]).Output);
            Check.True(next.Request().Body.GetRawText().Contains("observation", StringComparison.Ordinal), "Provider lost the tool result.");
            Check.Equal(1, events.OfType<AgentEvent.ToolExecutionStart>().Count());
            Check.Equal(1, events.OfType<AgentEvent.ToolExecutionEnd>().Count());
            Check.Equal("done\ncafé", result.Text);
            for (int i = 0; i < events.Count; i++) Check.Equal((long)i + 1, events[i].Sequence);
        }
    }

    private static async Task Recovery()
    {
        int requests = 0;
        int handled = 0;
        var tool = new Tool("echo", "", Parse("""{"properties":{"mode":{"type":"string"}},"required":["mode"]}"""), (args, _) =>
        {
            handled++;
            return args.GetProperty("mode").GetString() switch
            {
                "throw" => throw new IOException(new string('é', 100_000)),
                "large" => ValueTask.FromResult(new string('é', Tool.MaxOutputBytes)),
                "unicode" => ValueTask.FromResult("\ud800"),
                _ => ValueTask.FromResult("recovered")
            };
        });
        using var provider = new ScriptedProvider((request, _, _) =>
        {
            requests++;
            Check.Equal(1, request.Tools.Count);
            if (requests > 1) return Task.FromResult(Done());
            return Task.FromResult(Calls(new[] { "missing", "throw", "large", "unicode", "ok" }.Select((mode, i) =>
                new ToolCall(i.ToString(), "echo", mode == "missing" ? Parse("{}") : JsonSerializer.SerializeToElement(new { mode }))).ToArray()));
        });
        List<Message> previous = [new UserMessage("keep")];
        List<AgentEvent> events = [];
        var result = await new Agent(provider, "fixture", tools: [tool]).RunAsync("go", previous, (value, _) =>
        { events.Add(value); return ValueTask.CompletedTask; });
        Check.Equal(2, requests);
        Check.Equal(4, handled);
        Check.Equal(1, previous.Count);
        var observations = result.History.OfType<ToolResultMessage>().ToArray();
        Check.Equal(5, observations.Length);
        Check.True(observations.Take(4).All(value => value.IsError && Json.Utf8.GetByteCount(value.Output) <= 2000), "Errors were not bounded observations.");
        Check.Equal("recovered", observations[^1].Output);
        Check.True(!observations[^1].IsError, "Recovery failed.");
        var tools = events.Where(value => value is AgentEvent.ToolExecutionStart or AgentEvent.ToolExecutionEnd).ToArray();
        for (int i = 0; i < 5; i++)
        {
            Check.True(tools[2 * i] is AgentEvent.ToolExecutionStart, "Tools overlapped.");
            Check.Equal(i.ToString(), ((AgentEvent.ToolExecutionEnd)tools[2 * i + 1]).ToolCallId);
        }
    }

    private static async Task Cancellation()
    {
        foreach (bool fromStart in new[] { true, false })
        {
            using var parent = new CancellationTokenSource();
            bool cleaned = false;
            int handlers = 0;
            using var provider = new ScriptedProvider((_, _, _) => Task.FromResult(Calls(new ToolCall("c", "wait", Parse("{}")))));
            var wait = new Tool("wait", "", Parse("{}"), async (_, token) =>
            {
                handlers++;
                await parent.CancelAsync();
                try { await Task.Delay(Timeout.Infinite, token); }
                finally { await Task.Yield(); cleaned = true; }
                return "unreachable";
            });
            List<AgentEvent> events = [];
            await Check.ThrowsAsync<OperationCanceledException>(() => new Agent(provider, "fixture", tools: [wait]).RunAsync("go", onEvent: async (value, _) =>
            {
                events.Add(value);
                if (fromStart && value is AgentEvent.ToolExecutionStart) await parent.CancelAsync();
                if (!fromStart && value is AgentEvent.ToolExecutionEnd) Check.True(cleaned, "End preceded handler cleanup.");
            }, cancellationToken: parent.Token));
            Check.Equal(fromStart ? 0 : 1, handlers);
            Check.Equal("agent_start,tool_execution_start,tool_execution_end,agent_end", string.Join(',', events.Select(value => value.ToJson()["type"]!.GetValue<string>())));
            Check.True(((AgentEvent.ToolExecutionEnd)events[2]).IsError && events[^1] is AgentEvent.Aborted, "Cancellation did not pair tool events.");
        }
    }

    private static async Task ConsumerFailures()
    {
        foreach (bool atEnd in new[] { false, true })
        {
            int handlers = 0;
            using var provider = new ScriptedProvider((_, _, _) => Task.FromResult(Calls(new ToolCall("c", "echo", Parse("{}")))));
            var tool = new Tool("echo", "", Parse("{}"), (_, _) => { handlers++; return ValueTask.FromResult("ok"); });
            var failure = new IOException("consumer left");
            List<AgentEvent> events = [];
            var error = await Check.ThrowsAsync<IOException>(() => new Agent(provider, "fixture", tools: [tool]).RunAsync("go", onEvent: (value, _) =>
            {
                events.Add(value);
                if (atEnd ? value is AgentEvent.ToolExecutionEnd : value is AgentEvent.ToolExecutionStart) throw failure;
                return ValueTask.CompletedTask;
            }));
            Check.True(ReferenceEquals(failure, error), "Consumer failure was replaced.");
            Check.Equal(atEnd ? 1 : 0, handlers);
            Check.Equal(atEnd ? 3 : 2, events.Count);
        }
    }

    internal static async Task Transports(bool tcp = false)
    {
        using var provider = new ScriptedProvider((request, _, _) => Task.FromResult(request.History[^1] is ToolResultMessage
            ? Done() : Calls(new ToolCall("c", "echo", Parse("{}")))));
        var agent = new Agent(provider, "fixture", tools: [Echo()]);
        List<JsonObject> events = [];
        await using (var rpc = new RpcSession(agent, (value, _) => { events.Add(value); return ValueTask.CompletedTask; }))
        {
            await rpc.HandleAsync(new JsonObject { ["type"] = "prompt", ["message"] = "go" });
            await rpc.WaitForIdleAsync();
            Check.Equal("response", events[0]["type"]!.GetValue<string>());
            Check.Equal(1, events.Count(value => value["type"]!.GetValue<string>() == "tool_execution_end"));
        }
        await using var transport = new HttpTransportFixture();
        await using var server = tcp
            ? await HttpServer.StartAsync(agent, new HttpServerOptions { Port = 0 })
            : await HttpServer.StartAsync(agent, new(), transport);
        using var client = tcp ? new HttpClient { BaseAddress = server.Address, Timeout = TimeSpan.FromSeconds(5) } : transport.Client();
        using var created = await client.PostAsync("/sessions", null);
        var session = JsonNode.Parse(await created.Content.ReadAsStringAsync())!["sessionId"]!.GetValue<string>();
        using var response = await client.PostAsync($"/sessions/{session}/prompts", new StringContent("{\"message\":\"go\"}", System.Text.Encoding.UTF8, "application/json"));
        response.EnsureSuccessStatusCode();
        string body = await response.Content.ReadAsStringAsync();
        Check.True(body.Contains("tool_execution_end", StringComparison.Ordinal) && body.Contains("completed", StringComparison.Ordinal), "HTTP did not run the native tool loop.");
    }
}
