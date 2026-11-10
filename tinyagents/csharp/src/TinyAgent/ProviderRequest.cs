namespace TinyAgent;

public sealed class ProviderRequest
{
    public string Model { get; }
    public string Instructions { get; }
    public IReadOnlyList<Message> History { get; }
    public IReadOnlyList<ToolDefinition> Tools { get; }

    public ProviderRequest(
        string model,
        string instructions,
        IEnumerable<Message> history,
        IEnumerable<ToolDefinition>? tools = null)
    {
        ArgumentException.ThrowIfNullOrEmpty(model);
        ArgumentNullException.ThrowIfNull(instructions);
        Model = model;
        Instructions = instructions;
        History = Array.AsReadOnly(history.ToArray());
        Tools = Array.AsReadOnly(tools?.ToArray() ?? []);
    }
}
