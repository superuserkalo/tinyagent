using System.Text.Json;
using Json.Schema;

namespace TinyAgent;

/// <summary>A native handler with an immutable Draft 2020-12 argument schema.</summary>
public sealed class Tool
{
    public const int MaxOutputBytes = 131_072;
    public ToolDefinition Definition { get; }
    private readonly JsonSchema schema;
    private readonly Func<JsonElement, CancellationToken, ValueTask<string>> handler;

    public Tool(string name, string description, JsonElement parameters,
        Func<JsonElement, CancellationToken, ValueTask<string>> handler)
    {
        ArgumentNullException.ThrowIfNull(handler);
        Definition = new ToolDefinition(Json.Text(name, nameof(name)), Json.Text(description, nameof(description)), parameters);
        this.handler = handler;
        var options = new EvaluationOptions { RequireFormatValidation = false, OutputFormat = OutputFormat.Flag };
        if (!MetaSchemas.Draft202012.Evaluate(Definition.Parameters, options).IsValid)
            throw new ArgumentException("Tool parameters must be a valid Draft 2020-12 schema.", nameof(parameters));
        schema = JsonSchema.Build(Definition.Parameters, new BuildOptions
        {
            Dialect = Dialect.Draft202012,
            SchemaRegistry = new() { Fetch = (uri, _) => throw new ArgumentException($"External schema is not available: {uri}") }
        });
        ToolSchema.Check(schema.Root);
    }

    internal async ValueTask<string> ExecuteAsync(JsonElement arguments, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        if (!schema.Evaluate(arguments, new EvaluationOptions
            { RequireFormatValidation = false, OutputFormat = OutputFormat.Flag }).IsValid)
            throw new ArgumentException($"Invalid arguments for tool '{Definition.Name}'.");
        token.ThrowIfCancellationRequested();
        return await handler(arguments, token).ConfigureAwait(false);
    }
}
