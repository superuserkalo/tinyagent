using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace TinyAgent.Checks;

internal static class ProviderChecks
{
    internal static ProviderRequest Request(IEnumerable<Message>? history = null) => new(
        "fixture", "Instructions", history ?? [new UserMessage("go")],
        [new ToolDefinition("echo", "Echo text", JsonSerializer.SerializeToElement(new
        {
            type = "object",
            properties = new { text = new { type = "string" } },
            required = new[] { "text" }
        }))]);

    internal static IProvider Provider(bool anthropic, HttpClient client, TimeSpan? timeout = null) =>
        anthropic
            ? new AnthropicProvider("fixture-key", "http://fixture.invalid/api?mode=test", timeout, client: client)
            : new OpenAIProvider("fixture-key", "http://fixture.invalid/api?mode=test", timeout, client);

    internal static async Task OpenAIReplay()
    {
        using var fixture = new HttpFixture();
        var first = fixture.Enqueue(HttpFixture.Wire(HttpFixture.Sse("openai_tool")));
        var second = fixture.Enqueue(HttpFixture.Wire(HttpFixture.Sse("openai_text")));
        using var provider = new OpenAIProvider("fixture-key", "http://fixture.invalid/api?mode=test", client: fixture.Client);
        var deltas = new List<string>();
        var turn = await provider.StreamAsync(Request(), (delta, _) =>
        {
            deltas.Add(delta);
            return ValueTask.CompletedTask;
        });
        Check.Equal("Working ", string.Concat(deltas));
        Check.Equal(1, turn.ToolCalls.Count);
        var call = turn.ToolCalls[0];
        Check.Equal("call_1", call.CallId);
        Check.Equal("café", call.Arguments.GetProperty("text").GetString());
        var request = first.Request();
        Check.Equal("POST /api?mode=test HTTP/1.1", request.Line);
        Check.Equal("Bearer fixture-key", request.Headers["authorization"]);
        Check.True(request.Body.GetProperty("stream").GetBoolean(), "OpenAI did not request streaming.");
        Check.True(!request.Body.GetProperty("store").GetBoolean(), "OpenAI requested storage.");
        Check.Equal(1, request.Body.GetProperty("include").GetArrayLength());
        Check.Equal("reasoning.encrypted_content", request.Body.GetProperty("include")[0].GetString());
        Check.True(!request.Body.GetProperty("parallel_tool_calls").GetBoolean(), "OpenAI requested parallel tools.");
        Check.Equal("object", request.Body.GetProperty("tools")[0].GetProperty("parameters").GetProperty("type").GetString());
        deltas.Clear();
        var answer = await provider.StreamAsync(Request([
            new UserMessage("go"), turn.Assistant, new ToolResultMessage(call.CallId, call.Name, "café")
        ]), (delta, _) =>
        {
            deltas.Add(delta);
            return ValueTask.CompletedTask;
        });
        Check.Equal("done\ncafé", string.Concat(deltas));
        Check.Equal("done\ncafé", answer.Text);
        var input = second.Request().Body.GetProperty("input");
        Check.Equal("reasoning.encrypted_content", second.Request().Body.GetProperty("include")[0].GetString());
        Check.Equal("opaque-fixture", input[1].GetProperty("encrypted_content").GetString());
        using var arguments = JsonDocument.Parse(input[2].GetProperty("arguments").GetString()!);
        Check.Equal("café", arguments.RootElement.GetProperty("text").GetString());
        Check.Equal("function_call_output", input[3].GetProperty("type").GetString());
        await Task.WhenAll(first.Closed.Task, second.Closed.Task).WaitAsync(TimeSpan.FromSeconds(2));
    }

    internal static async Task AnthropicReplay()
    {
        using var fixture = new HttpFixture();
        var first = fixture.Enqueue(HttpFixture.Wire(HttpFixture.Sse("anthropic_tool")));
        var second = fixture.Enqueue(HttpFixture.Wire(HttpFixture.Sse("anthropic_text")));
        using var provider = new AnthropicProvider("fixture-key", "http://fixture.invalid/api", client: fixture.Client);
        var turn = await provider.StreamAsync(Request());
        Check.Equal("café", turn.ToolCalls[0].Arguments.GetProperty("text").GetString());
        var replay = turn.Replay[0];
        Check.Equal("signed-fixture", replay.GetProperty("content")[0].GetProperty("signature").GetString());
        Check.Equal("keep", replay.GetProperty("fixture_metadata").GetString());
        Check.Equal(8, replay.GetProperty("usage").GetProperty("input_tokens").GetInt32());
        Check.Equal(12, replay.GetProperty("usage").GetProperty("output_tokens").GetInt32());
        Check.Equal("fixture-key", first.Request().Headers["x-api-key"]);
        Check.Equal("2023-06-01", first.Request().Headers["anthropic-version"]);
        var deltas = new List<string>();
        var answer = await provider.StreamAsync(Request([
            new UserMessage("go"), turn.Assistant,
            new ToolResultMessage("call_1", "echo", "café"),
            new ToolResultMessage("call_2", "echo", "failed", true)
        ]), (delta, _) =>
        {
            deltas.Add(delta);
            return ValueTask.CompletedTask;
        });
        Check.Equal("done\ncafé", answer.Text);
        Check.Equal("done\ncafé", string.Concat(deltas));
        var messages = second.Request().Body.GetProperty("messages");
        Check.Equal("signed-fixture", messages[1].GetProperty("content")[0].GetProperty("signature").GetString());
        Check.Equal(2, messages[2].GetProperty("content").GetArrayLength());
        Check.True(messages[2].GetProperty("content")[1].GetProperty("is_error").GetBoolean(), "Tool error flag was lost.");
        await Task.WhenAll(first.Closed.Task, second.Closed.Task).WaitAsync(TimeSpan.FromSeconds(2));
    }

    internal static async Task ImmutableValues()
    {
        using var document = JsonDocument.Parse("{\"nested\":{\"text\":\"keep\"}}");
        var call = new ToolCall("c", "echo", document.RootElement);
        var definition = new ToolDefinition("echo", "Echo", document.RootElement);
        var replay = new[] { document.RootElement };
        var calls = new List<ToolCall> { call };
        var assistant = new AssistantMessage("text", calls, replay);
        var turn = new ProviderTurn("text", calls, replay, replay);
        var history = new List<Message> { assistant };
        var tools = new List<ToolDefinition> { definition };
        var request = new ProviderRequest("fixture", "", history, tools);
        calls.Clear();
        history.Clear();
        tools.Clear();
        document.Dispose();
        Check.Equal("keep", call.Arguments.GetProperty("nested").GetProperty("text").GetString());
        Check.Equal("keep", definition.Parameters.GetProperty("nested").GetProperty("text").GetString());
        Check.Equal(1, assistant.ToolCalls.Count);
        Check.Equal("keep", assistant.Replay[0].GetProperty("nested").GetProperty("text").GetString());
        Check.Equal(1, request.History.Count);
        Check.Equal(1, request.Tools.Count);
        Check.Equal("keep", turn.Output[0].GetProperty("nested").GetProperty("text").GetString());
        await Check.ThrowsAsync<NotSupportedException>(() =>
        {
            ((IList<ToolCall>)turn.ToolCalls).Clear();
            return Task.CompletedTask;
        });
    }

    internal static async Task FramingAndBounds()
    {
        var completed = "{\"type\":\"response.completed\",\"response\":{\"output\":[]}}";
        string valid = ": comment\r\nevent: ignored\r\ndata: {\"type\":\"response.completed\",\r\n" +
            "data: \"response\":{\"output\":[]}}\r\n\r\n";
        using var fixture = new HttpFixture();
        fixture.Enqueue(HttpFixture.Wire(valid));
        using var provider = new OpenAIProvider("fixture-key", "http://fixture.invalid/api", client: fixture.Client);
        Check.Equal("", (await provider.StreamAsync(Request())).Text);
        foreach (var invalid in new[]
        {
            "data: []\n\n", "data: {\"x\":NaN}\n\n", "data: [DONE]\n\n",
            "data: {\"type\":\"response.output_text.delta\",\"delta\":42}\n\n",
            new string('x', 1_048_577),
            "data: " + new string(' ', 600_000) + "\ndata: " + new string(' ', 600_000) + "\n"
        })
        {
            using var badFixture = new HttpFixture();
            var stream = badFixture.Enqueue(HttpFixture.Wire(invalid), fragment: 8192);
            using var badProvider = new OpenAIProvider("fixture-key", "http://fixture.invalid/api", client: badFixture.Client);
            await Check.ThrowsAsync<ProviderException>(() => badProvider.StreamAsync(Request()));
            await stream.Closed.Task.WaitAsync(TimeSpan.FromSeconds(2));
        }
        using var invalidUtf8 = new HttpFixture();
        byte[] prefix = Encoding.ASCII.GetBytes("HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\ndata: ");
        invalidUtf8.Enqueue([.. prefix, 0xff, (byte)'\n', (byte)'\n']);
        using var utf8Provider = new OpenAIProvider("fixture-key", "http://fixture.invalid/api", client: invalidUtf8.Client);
        await Check.ThrowsAsync<ProviderException>(() => utf8Provider.StreamAsync(Request()));
        using var eofFixture = new HttpFixture();
        eofFixture.Enqueue(HttpFixture.Wire("data: " + completed, chunked: false));
        using var eofProvider = new OpenAIProvider("fixture-key", "http://fixture.invalid/api", client: eofFixture.Client);
        Check.Equal("", (await eofProvider.StreamAsync(Request())).Text);
    }

    internal static async Task AccumulatedArgumentBounds()
    {
        string fragment = new(' ', 600_000);
        foreach (bool anthropic in new[] { false, true })
        {
            JsonObject delta = anthropic
                ? new JsonObject
                {
                    ["type"] = "content_block_delta", ["index"] = 0,
                    ["delta"] = new JsonObject { ["type"] = "input_json_delta", ["partial_json"] = fragment }
                }
                : new JsonObject
                {
                    ["type"] = "response.function_call_arguments.delta", ["output_index"] = 0, ["delta"] = fragment
                };
            string prefix = anthropic ? HttpFixture.Events(
                new JsonObject { ["type"] = "message_start", ["message"] = new JsonObject() },
                new JsonObject
                {
                    ["type"] = "content_block_start", ["index"] = 0,
                    ["content_block"] = new JsonObject { ["type"] = "tool_use", ["input"] = new JsonObject() }
                }) : "";
            using var fixture = new HttpFixture();
            var stream = fixture.Enqueue(HttpFixture.Wire(prefix + HttpFixture.Events(delta, delta)), fragment: 8192);
            using var provider = Provider(anthropic, fixture.Client);
            var error = await Check.ThrowsAsync<ProviderException>(() => provider.StreamAsync(Request()));
            Check.True(error.Message.Contains("Tool arguments", StringComparison.Ordinal), "The cumulative argument limit was not enforced.");
            await stream.Closed.Task.WaitAsync(TimeSpan.FromSeconds(2));
        }
    }

    internal static async Task MalformedOpenAI()
    {
        var call = new JsonObject
        {
            ["type"] = "function_call", ["call_id"] = "c", ["name"] = "echo", ["arguments"] = "{}"
        };
        var invalid = new[]
        {
            "{\"type\":\"response.function_call_arguments.delta\",\"output_index\":-1,\"delta\":\"{}\"}",
            "{\"type\":\"response.function_call_arguments.delta\",\"output_index\":true,\"delta\":\"{}\"}",
            "{\"type\":\"response.completed\",\"response\":{\"status\":\"incomplete\",\"output\":[]}}",
            "{\"type\":\"response.completed\",\"response\":{\"output\":[null]}}",
            "{\"type\":\"response.failed\",\"response\":{\"error\":{\"message\":\"failed\"}}}",
            "{\"type\":\"response.output_text.delta\",\"delta\":\"partial\"}"
        };
        foreach (var value in invalid)
            await Reject(false, "data: " + value + "\n\n");
        await Reject(false, HttpFixture.Events(new JsonObject
        {
            ["type"] = "response.completed",
            ["response"] = new JsonObject { ["output"] = new JsonArray(call.DeepClone(), call.DeepClone()) }
        }));
        call["arguments"] = "[]";
        await Reject(false, HttpFixture.Events(new JsonObject
        {
            ["type"] = "response.completed", ["response"] = new JsonObject { ["output"] = new JsonArray(call.DeepClone()) }
        }));
        call["arguments"] = "{}";
        using var fixture = new HttpFixture();
        fixture.Enqueue(HttpFixture.Wire(HttpFixture.Events(
            new JsonObject { ["type"] = "response.output_item.added", ["output_index"] = 100, ["item"] = call },
            new JsonObject { ["type"] = "response.completed", ["response"] = new JsonObject() })));
        using var provider = new OpenAIProvider("fixture-key", "http://fixture.invalid/api", client: fixture.Client);
        Check.Equal("c", (await provider.StreamAsync(Request())).ToolCalls[0].CallId);
    }

    internal static async Task MalformedAnthropic()
    {
        string body = HttpFixture.Sse("anthropic_tool");
        foreach (string invalid in new[]
        {
            "data: {\"type\":\"message_stop\"}\n\n",
            "data: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n",
            body.Replace("\"stop_reason\":\"tool_use\"", "\"stop_reason\":\"max_tokens\"", StringComparison.Ordinal),
            body.Replace("\"type\":\"signature_delta\"", "\"type\":\"text_delta\"", StringComparison.Ordinal),
            body.Replace("data: {\"type\":\"content_block_stop\",\"index\":1}", ": missing stop", StringComparison.Ordinal),
            body.Replace("\"type\":\"input_json_delta\"", "\"type\":\"thinking_delta\"", StringComparison.Ordinal)
        })
            await Reject(true, invalid);
    }

    private static async Task Reject(bool anthropic, string body)
    {
        using var fixture = new HttpFixture();
        var stream = fixture.Enqueue(HttpFixture.Wire(body));
        using var provider = Provider(anthropic, fixture.Client);
        await Check.ThrowsAsync<ProviderException>(() => provider.StreamAsync(Request()));
        await stream.Closed.Task.WaitAsync(TimeSpan.FromSeconds(2));
    }
}
