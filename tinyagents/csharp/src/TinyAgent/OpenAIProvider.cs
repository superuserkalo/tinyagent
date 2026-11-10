using System.Text;
using System.Text.Json.Nodes;

namespace TinyAgent;

public sealed class OpenAIProvider : IProvider
{
    private readonly string? apiKey;
    private readonly HttpStream http;

    public OpenAIProvider(
        string? apiKey = null,
        string endpoint = "https://api.openai.com/v1/responses",
        TimeSpan? timeout = null,
        HttpClient? client = null)
    {
        this.apiKey = apiKey;
        http = new HttpStream(endpoint, timeout, client);
    }

    public async Task<ProviderTurn> StreamAsync(
        ProviderRequest request,
        TextDeltaHandler? onText = null,
        CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        string? key = apiKey ?? Environment.GetEnvironmentVariable("OPENAI_API_KEY");
        if (string.IsNullOrWhiteSpace(key))
            throw new ProviderException("OPENAI_API_KEY is not set.");
        return await http.SendAsync(
            Serialize(request),
            new Dictionary<string, string> { ["Authorization"] = $"Bearer {key}" },
            new ResponseParser(), onText, cancellationToken).ConfigureAwait(false);
    }

    public void Dispose() => http.Dispose();

    private static JsonObject Serialize(ProviderRequest request)
    {
        var input = new JsonArray();
        foreach (var message in request.History)
        {
            switch (message)
            {
                case UserMessage user:
                    input.Add(new JsonObject { ["role"] = "user", ["content"] = user.Text });
                    break;
                case AssistantMessage assistant:
                    if (assistant.Replay.Count > 0)
                    {
                        foreach (var item in assistant.Replay)
                            input.Add(Json.Node(item));
                        break;
                    }
                    if (assistant.Text.Length > 0)
                        input.Add(new JsonObject { ["role"] = "assistant", ["content"] = assistant.Text });
                    foreach (var call in assistant.ToolCalls)
                        input.Add(new JsonObject
                        {
                            ["type"] = "function_call", ["call_id"] = call.CallId,
                            ["name"] = call.Name, ["arguments"] = call.Arguments.GetRawText()
                        });
                    break;
                case ToolResultMessage result:
                    input.Add(new JsonObject
                    {
                        ["type"] = "function_call_output", ["call_id"] = result.CallId, ["output"] = result.Output
                    });
                    break;
                default:
                    throw new ProviderException("Unsupported history message.");
            }
        }
        var tools = new JsonArray();
        foreach (var tool in request.Tools)
            tools.Add(new JsonObject
            {
                ["type"] = "function", ["name"] = tool.Name, ["description"] = tool.Description,
                ["parameters"] = Json.Node(tool.Parameters), ["strict"] = true
            });
        return new JsonObject
        {
            ["model"] = request.Model, ["instructions"] = request.Instructions, ["input"] = input,
            ["tools"] = tools, ["parallel_tool_calls"] = false, ["store"] = false, ["stream"] = true,
            ["include"] = new JsonArray("reasoning.encrypted_content")
        };
    }

    private sealed class ResponseParser : IResponseParser
    {
        private readonly SortedDictionary<int, JsonObject> output = new();
        private readonly Dictionary<int, Arguments> arguments = new();

        public async ValueTask<ProviderTurn?> ConsumeAsync(
            JsonObject value, TextDeltaHandler? onText, CancellationToken cancellationToken)
        {
            string kind = Json.String(value, "type", true);
            switch (kind)
            {
                case "response.output_item.added":
                case "response.output_item.done":
                {
                    int index = Json.Index(value["output_index"]);
                    var item = (JsonObject)Json.Object(value["item"], "output item").DeepClone();
                    if (Json.String(item, "type", true) == "function_call")
                    {
                        string text = Json.String(item, "arguments");
                        if (text.Length > 0)
                            Buffer(index).Set(text);
                    }
                    output[index] = item;
                    break;
                }
                case "response.output_text.delta":
                {
                    string delta = Json.String(value, "delta");
                    if (delta.Length > 0 && onText is not null)
                        await onText(delta, cancellationToken).ConfigureAwait(false);
                    break;
                }
                case "response.function_call_arguments.delta":
                    Buffer(Json.Index(value["output_index"])).Append(Json.String(value, "delta"));
                    break;
                case "response.function_call_arguments.done":
                    Buffer(Json.Index(value["output_index"])).Set(Json.String(value, "arguments"));
                    break;
                case "response.completed":
                {
                    var response = Json.Object(value["response"], "completed response");
                    if (response.ContainsKey("status") && Json.String(response, "status") != "completed")
                        throw new ProviderException("OpenAI response is incomplete.");
                    if (response.ContainsKey("output"))
                    {
                        output.Clear();
                        int index = 0;
                        foreach (var item in Json.Array(response["output"], "output"))
                            output[index++] = (JsonObject)Json.Object(item, "output item").DeepClone();
                    }
                    return Complete();
                }
                case "response.failed":
                case "response.incomplete":
                case "error":
                {
                    var detail = value["response"]?["error"]?["message"] ?? value["message"];
                    string message = detail is JsonValue text && text.TryGetValue<string>(out var reason)
                        ? reason : $"OpenAI stream failed: {kind}";
                    throw new ProviderException(message);
                }
            }
            return null;
        }

        private Arguments Buffer(int index)
        {
            if (!arguments.TryGetValue(index, out var buffer))
            {
                buffer = new Arguments();
                arguments.Add(index, buffer);
            }
            return buffer;
        }

        private ProviderTurn Complete()
        {
            var calls = new List<ToolCall>();
            var ids = new HashSet<string>();
            var text = new StringBuilder();
            foreach (var (index, item) in output)
            {
                switch (Json.String(item, "type", true))
                {
                    case "function_call":
                    {
                        string name = Json.String(item, "name", true);
                        string id = Json.String(item, "call_id", true);
                        if (!ids.Add(id))
                            throw new ProviderException("Duplicate OpenAI tool call.");
                        string raw = Json.String(item, "arguments");
                        if (raw.Length == 0 && arguments.TryGetValue(index, out var buffer))
                            raw = buffer.ToString();
                        if (Json.Utf8.GetByteCount(raw) > Json.MaxSseBytes)
                            throw new ProviderException("Tool arguments exceed their byte limit.");
                        var parsed = Json.ParseObject(raw);
                        item["arguments"] = raw;
                        calls.Add(new ToolCall(id, name, Json.Element(parsed)));
                        break;
                    }
                    case "message":
                        foreach (var child in Json.Array(item["content"], "message content"))
                        {
                            var part = Json.Object(child, "message content");
                            if (Json.String(part, "type", true) == "output_text")
                                text.Append(Json.String(part, "text"));
                        }
                        break;
                }
            }
            var items = output.Values.Select(Json.Element).ToArray();
            return new ProviderTurn(text.ToString(), calls, items, items);
        }
    }
}
