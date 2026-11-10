using System.Text;
using System.Text.Json.Nodes;

namespace TinyAgent;

public sealed class AnthropicProvider : IProvider
{
    private readonly string? apiKey;
    private readonly int maxTokens;
    private readonly HttpStream http;

    public AnthropicProvider(
        string? apiKey = null,
        string endpoint = "https://api.anthropic.com/v1/messages",
        TimeSpan? timeout = null,
        int maxTokens = 8192,
        HttpClient? client = null)
    {
        ArgumentOutOfRangeException.ThrowIfNegativeOrZero(maxTokens);
        this.apiKey = apiKey;
        this.maxTokens = maxTokens;
        http = new HttpStream(endpoint, timeout, client);
    }

    public async Task<ProviderTurn> StreamAsync(
        ProviderRequest request,
        TextDeltaHandler? onText = null,
        CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        string? key = apiKey ?? Environment.GetEnvironmentVariable("ANTHROPIC_API_KEY");
        if (string.IsNullOrWhiteSpace(key))
            throw new ProviderException("ANTHROPIC_API_KEY is not set.");
        return await http.SendAsync(
            Serialize(request),
            new Dictionary<string, string> { ["x-api-key"] = key, ["anthropic-version"] = "2023-06-01" },
            new ResponseParser(), onText, cancellationToken).ConfigureAwait(false);
    }

    public void Dispose() => http.Dispose();

    private JsonObject Serialize(ProviderRequest request)
    {
        var messages = new JsonArray();
        foreach (var message in request.History)
        {
            switch (message)
            {
                case UserMessage user:
                    messages.Add(new JsonObject { ["role"] = "user", ["content"] = user.Text });
                    break;
                case AssistantMessage assistant:
                {
                    var content = new JsonArray();
                    if (assistant.Replay.Count > 0)
                    {
                        var replay = Json.Object(Json.Node(assistant.Replay[0]), "Anthropic replay");
                        content = (JsonArray)Json.Array(replay["content"], "Anthropic replay content").DeepClone();
                    }
                    else
                    {
                        if (assistant.Text.Length > 0)
                            content.Add(new JsonObject { ["type"] = "text", ["text"] = assistant.Text });
                        foreach (var call in assistant.ToolCalls)
                            content.Add(new JsonObject
                            {
                                ["type"] = "tool_use", ["id"] = call.CallId,
                                ["name"] = call.Name, ["input"] = Json.Node(call.Arguments)
                            });
                    }
                    messages.Add(new JsonObject { ["role"] = "assistant", ["content"] = content });
                    break;
                }
                case ToolResultMessage result:
                {
                    var block = new JsonObject
                    {
                        ["type"] = "tool_result", ["tool_use_id"] = result.CallId,
                        ["content"] = result.Output, ["is_error"] = result.IsError
                    };
                    var previous = messages.LastOrDefault() as JsonObject;
                    if (previous?["role"]?.GetValue<string>() == "user" && previous["content"] is JsonArray blocks &&
                        blocks.All(item => item?["type"]?.GetValue<string>() == "tool_result"))
                        blocks.Add(block);
                    else
                        messages.Add(new JsonObject { ["role"] = "user", ["content"] = new JsonArray(block) });
                    break;
                }
                default:
                    throw new ProviderException("Unsupported history message.");
            }
        }
        var payload = new JsonObject
        {
            ["model"] = request.Model, ["messages"] = messages, ["max_tokens"] = maxTokens, ["stream"] = true
        };
        if (request.Instructions.Length > 0)
            payload["system"] = request.Instructions;
        if (request.Tools.Count > 0)
        {
            var tools = new JsonArray();
            foreach (var tool in request.Tools)
                tools.Add(new JsonObject
                {
                    ["name"] = tool.Name, ["description"] = tool.Description, ["input_schema"] = Json.Node(tool.Parameters)
                });
            payload["tools"] = tools;
        }
        return payload;
    }

    private sealed class Block(JsonObject value)
    {
        internal JsonObject Value { get; } = value;
        internal Arguments Arguments { get; } = new();
        internal bool Open { get; set; } = true;
    }

    private sealed class ResponseParser : IResponseParser
    {
        private readonly SortedDictionary<int, Block> blocks = new();
        private JsonObject? message;
        private readonly JsonObject usage = new();

        public async ValueTask<ProviderTurn?> ConsumeAsync(
            JsonObject value, TextDeltaHandler? onText, CancellationToken cancellationToken)
        {
            string kind = Json.String(value, "type", true);
            switch (kind)
            {
                case "message_start":
                    if (message is not null)
                        throw new ProviderException("Repeated Anthropic message_start.");
                    message = (JsonObject)Json.Object(value["message"], "message").DeepClone();
                    if (message.ContainsKey("usage"))
                        Merge(usage, Json.Object(message["usage"], "usage"));
                    break;
                case "content_block_start":
                {
                    int index = Json.Index(value["index"]);
                    if (message is null || blocks.ContainsKey(index))
                        throw new ProviderException("Invalid Anthropic block order.");
                    var item = (JsonObject)Json.Object(value["content_block"], "content block").DeepClone();
                    Json.String(item, "type", true);
                    blocks[index] = new Block(item);
                    break;
                }
                case "content_block_delta":
                case "content_block_stop":
                {
                    int index = Json.Index(value["index"]);
                    if (!blocks.TryGetValue(index, out var block) || !block.Open)
                        throw new ProviderException("Anthropic event referenced an unopened block.");
                    if (kind == "content_block_stop")
                    {
                        if (Json.String(block.Value, "type", true) == "tool_use")
                        {
                            string raw = block.Arguments.ToString();
                            if (raw.Length > 0)
                                block.Value["input"] = Json.ParseObject(raw);
                            Json.Object(block.Value["input"], "tool arguments");
                        }
                        block.Open = false;
                        break;
                    }
                    var delta = Json.Object(value["delta"], "block delta");
                    string deltaKind = Json.String(delta, "type", true);
                    (string Type, string Field)? fields = deltaKind switch
                    {
                        "text_delta" => ("text", "text"),
                        "thinking_delta" => ("thinking", "thinking"),
                        "signature_delta" => ("thinking", "signature"),
                        "input_json_delta" => ("tool_use", "partial_json"),
                        _ => null
                    };
                    if (fields is null)
                        break;
                    var (expected, field) = fields.Value;
                    if (Json.String(block.Value, "type", true) != expected)
                        throw new ProviderException("Anthropic delta does not match its block.");
                    string addition = Json.String(delta, field);
                    if (deltaKind == "input_json_delta")
                        block.Arguments.Append(addition);
                    else
                    {
                        string previous = block.Value.ContainsKey(field) ? Json.String(block.Value, field) : "";
                        block.Value[field] = previous + addition;
                        if (deltaKind == "text_delta" && addition.Length > 0 && onText is not null)
                            await onText(addition, cancellationToken).ConfigureAwait(false);
                    }
                    break;
                }
                case "message_delta":
                    if (message is null)
                        throw new ProviderException("Anthropic delta preceded message_start.");
                    Merge(message, Json.Object(value["delta"], "message delta"));
                    if (value.ContainsKey("usage"))
                        Merge(usage, Json.Object(value["usage"], "usage delta"));
                    break;
                case "message_stop":
                    return Complete();
                case "error":
                {
                    var error = Json.Object(value["error"], "stream error");
                    throw new ProviderException(error.ContainsKey("message") ? Json.String(error, "message") : "Anthropic stream error.");
                }
            }
            return null;
        }

        private ProviderTurn Complete()
        {
            if (message is null)
                throw new ProviderException("Anthropic stream ended before a complete message.");
            string reason = Json.String(message, "stop_reason", true);
            if (reason is not ("end_turn" or "tool_use" or "stop_sequence" or "refusal"))
                throw new ProviderException($"Anthropic response is incomplete: {reason}");
            var calls = new List<ToolCall>();
            var ids = new HashSet<string>();
            var text = new StringBuilder();
            var output = new JsonArray();
            foreach (var block in blocks.Values)
            {
                if (block.Open)
                    throw new ProviderException("Anthropic stream ended with an unfinished content block.");
                var item = block.Value;
                switch (Json.String(item, "type", true))
                {
                    case "tool_use":
                    {
                        string id = Json.String(item, "id", true);
                        string name = Json.String(item, "name", true);
                        if (!ids.Add(id))
                            throw new ProviderException("Duplicate Anthropic tool call.");
                        var arguments = Json.Object(item["input"], "tool arguments");
                        if (Json.Utf8.GetByteCount(arguments.ToJsonString()) > Json.MaxSseBytes)
                            throw new ProviderException("Tool arguments exceed their byte limit.");
                        calls.Add(new ToolCall(id, name, Json.Element(arguments)));
                        break;
                    }
                    case "text":
                        text.Append(Json.String(item, "text"));
                        break;
                }
                output.Add(item.DeepClone());
            }
            var replay = (JsonObject)message.DeepClone();
            replay["content"] = output;
            replay["usage"] = usage.DeepClone();
            return new ProviderTurn(
                text.ToString(), calls, [Json.Element(replay)],
                output.Select(item => Json.Element(Json.Object(item, "output block"))));
        }

        private static void Merge(JsonObject target, JsonObject update)
        {
            foreach (var (key, value) in update)
                target[key] = value?.DeepClone();
        }
    }
}
