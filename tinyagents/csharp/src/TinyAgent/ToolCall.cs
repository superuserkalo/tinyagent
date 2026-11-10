using System.Text.Json;

namespace TinyAgent;

public sealed class ToolCall
{
    public string CallId { get; }
    public string Name { get; }
    public JsonElement Arguments { get; }

    public ToolCall(string callId, string name, JsonElement arguments)
    {
        ArgumentException.ThrowIfNullOrEmpty(callId);
        ArgumentException.ThrowIfNullOrEmpty(name);
        if (arguments.ValueKind != JsonValueKind.Object)
            throw new ArgumentException("Tool arguments must be a JSON object.", nameof(arguments));
        CallId = callId;
        Name = name;
        Arguments = arguments.Clone();
    }
}
