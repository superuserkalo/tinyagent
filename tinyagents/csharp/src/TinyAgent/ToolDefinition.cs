using System.Text.Json;

namespace TinyAgent;

/// <summary>Provider metadata; this definition neither executes tools nor validates schemas.</summary>
public sealed class ToolDefinition
{
    public string Name { get; }
    public string Description { get; }
    public JsonElement Parameters { get; }

    public ToolDefinition(string name, string description, JsonElement parameters)
    {
        ArgumentException.ThrowIfNullOrEmpty(name);
        ArgumentNullException.ThrowIfNull(description);
        if (parameters.ValueKind is not (JsonValueKind.Object or JsonValueKind.True or JsonValueKind.False))
            throw new ArgumentException("Tool parameters must be a schema object or boolean.", nameof(parameters));
        Name = name;
        Description = description;
        Parameters = parameters.Clone();
    }
}
