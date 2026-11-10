using System.Text.Json;

namespace TinyAgent;

public sealed class ProviderTurn
{
    public string Text { get; }
    public IReadOnlyList<ToolCall> ToolCalls { get; }
    public IReadOnlyList<JsonElement> Replay { get; }
    public IReadOnlyList<JsonElement> Output { get; }
    public AssistantMessage Assistant { get; }

    public ProviderTurn(
        string text,
        IEnumerable<ToolCall> toolCalls,
        IEnumerable<JsonElement> replay,
        IEnumerable<JsonElement> output)
    {
        ArgumentNullException.ThrowIfNull(text);
        Text = text;
        ToolCalls = Array.AsReadOnly(toolCalls.ToArray());
        Replay = Json.Snapshot(replay);
        Output = Json.Snapshot(output);
        Assistant = new AssistantMessage(text, ToolCalls, Replay);
    }
}
