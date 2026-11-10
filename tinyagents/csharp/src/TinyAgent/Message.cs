using System.Text.Json;

namespace TinyAgent;

public abstract record Message;

public sealed record UserMessage : Message
{
    public string Text { get; }

    public UserMessage(string text)
    {
        ArgumentNullException.ThrowIfNull(text);
        Text = text;
    }
}

public sealed record AssistantMessage : Message
{
    public string Text { get; }
    public IReadOnlyList<ToolCall> ToolCalls { get; }
    public IReadOnlyList<JsonElement> Replay { get; }

    public AssistantMessage(string text, IEnumerable<ToolCall> toolCalls, IEnumerable<JsonElement> replay)
    {
        ArgumentNullException.ThrowIfNull(text);
        Text = text;
        ToolCalls = Array.AsReadOnly(toolCalls.ToArray());
        Replay = Json.Snapshot(replay);
    }
}

public sealed record ToolResultMessage : Message
{
    public string CallId { get; }
    public string Name { get; }
    public string Output { get; }
    public bool IsError { get; }

    public ToolResultMessage(string callId, string name, string output, bool isError = false)
    {
        ArgumentException.ThrowIfNullOrEmpty(callId);
        ArgumentException.ThrowIfNullOrEmpty(name);
        ArgumentNullException.ThrowIfNull(output);
        CallId = callId;
        Name = name;
        Output = output;
        IsError = isError;
    }
}
