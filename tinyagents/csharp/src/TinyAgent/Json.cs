using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace TinyAgent;

internal static class Json
{
    internal const int MaxSseBytes = 1_048_576;
    internal static readonly UTF8Encoding Utf8 = new(false, true);

    internal static string Text(string value, string name)
    {
        ArgumentNullException.ThrowIfNull(value, name);
        try
        {
            Utf8.GetByteCount(value);
        }
        catch (EncoderFallbackException error)
        {
            throw new ArgumentException("Text contains an unpaired surrogate.", name, error);
        }
        return value;
    }

    internal static JsonObject ParseObject(string source)
    {
        try
        {
            return Object(JsonNode.Parse(source, documentOptions: new JsonDocumentOptions { MaxDepth = 100 }), "JSON");
        }
        catch (JsonException error)
        {
            throw new ProviderException("Invalid provider JSON.", error);
        }
    }

    internal static JsonObject Object(JsonNode? value, string name)
    {
        return value as JsonObject ?? throw new ProviderException($"Missing or invalid provider object: {name}");
    }

    internal static JsonArray Array(JsonNode? value, string name)
    {
        return value as JsonArray ?? throw new ProviderException($"Missing or invalid provider array: {name}");
    }

    internal static string String(JsonObject value, string field, bool required = false)
    {
        if (value[field] is not JsonValue child || !child.TryGetValue<string>(out var text) ||
            text is null || (required && text.Length == 0))
            throw new ProviderException($"Missing or invalid provider string: {field}");
        try
        {
            Utf8.GetByteCount(text);
        }
        catch (EncoderFallbackException error)
        {
            throw new ProviderException($"Invalid Unicode in provider string: {field}", error);
        }
        return text;
    }

    internal static int Index(JsonNode? value)
    {
        if (value is not JsonValue child || !child.TryGetValue<int>(out var index) || index < 0)
            throw new ProviderException("Missing or invalid provider block index.");
        return index;
    }

    internal static JsonElement Element(JsonNode value) => JsonSerializer.SerializeToElement(value);

    internal static JsonNode? Node(JsonElement value) => JsonNode.Parse(value.GetRawText());

    internal static IReadOnlyList<JsonElement> Snapshot(IEnumerable<JsonElement> values) =>
        System.Array.AsReadOnly(values.Select(value => value.Clone()).ToArray());
}

internal sealed class Arguments
{
    private readonly StringBuilder value = new();
    private int bytes;

    internal void Append(string delta)
    {
        int added = Json.Utf8.GetByteCount(delta);
        if (added > Json.MaxSseBytes - bytes)
            throw new ProviderException("Tool arguments exceed their byte limit.");
        bytes += added;
        value.Append(delta);
    }

    internal void Set(string text)
    {
        value.Clear();
        bytes = 0;
        Append(text);
    }

    public override string ToString() => value.ToString();
}
