using System.Text.Json;

namespace TinyAgent;

/// <summary>A completed answer and replayable history, isolated from caller mutation.</summary>
public sealed class RunResult
{
    public string Text { get; }
    public IReadOnlyList<Message> History { get; }
    public IReadOnlyList<JsonElement> Output { get; }

    internal RunResult(string text, IEnumerable<Message> history, IEnumerable<JsonElement> output)
    {
        Text = Json.Text(text, nameof(text));
        History = Array.AsReadOnly(history.ToArray());
        Output = Json.Snapshot(output);
    }
}
