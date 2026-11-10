using System.Text.Json;
using Json.Schema;

namespace TinyAgent;

// Inspect the validator's compiled graph, including local references. Older MIT
// releases do not reject every unresolved reference or non-progressing cycle.
internal static class ToolSchema
{
    internal static void Check(JsonSchemaNode root)
    {
        HashSet<JsonSchemaNode> owned = [];
        Collect(root);
        HashSet<JsonSchemaNode> checkedNodes = [];
        foreach (var node in owned) Visit(node, []);

        void Collect(JsonSchemaNode node)
        {
            if (!owned.Add(node)) return;
            if (node.Source.ValueKind == JsonValueKind.Object && node.Source.TryGetProperty("$schema", out var dialect)
                && dialect.GetString() != "https://json-schema.org/draft/2020-12/schema")
                throw new ArgumentException("Tool schemas must use Draft 2020-12.");
            foreach (var keyword in node.Keywords)
                if (keyword.Handler.Name is not ("$ref" or "$dynamicRef"))
                    foreach (var child in keyword.Subschemas) Collect(child);
        }

        void Visit(JsonSchemaNode node, HashSet<JsonSchemaNode> active)
        {
            if (active.Contains(node)) throw new ArgumentException("Tool schema has a reference cycle that does not descend into its input.");
            if (!checkedNodes.Add(node)) return;
            active.Add(node);
            foreach (var keyword in node.Keywords)
            {
                bool reference = keyword.Handler.Name is "$ref" or "$dynamicRef";
                IEnumerable<JsonSchemaNode> children = keyword.Subschemas;
                if (keyword.Handler.Name == "$dynamicRef" && keyword.Subschemas.Length == 0)
                {
                    // Dynamic anchors are resolved during evaluation, not stored as graph edges.
                    var target = new Uri(node.BaseUri, keyword.RawValue.GetString());
                    children = owned.Where(child => child.BaseUri == target && child.Source.ValueKind == JsonValueKind.Object
                        && child.Source.TryGetProperty("$dynamicAnchor", out var anchor)
                        && "#" + anchor.GetString() == target.Fragment).ToArray();
                }
                if (reference && (!children.Any() || children.Any(child => !owned.Contains(child))))
                    throw new ArgumentException($"Tool schema reference must resolve inside the supplied schema: {keyword.RawValue}");
                bool sameInput = reference || keyword.Handler.Name is "allOf" or "anyOf" or "oneOf"
                    or "not" or "if" or "then" or "else" or "dependentSchemas";
                if (sameInput)
                    foreach (var child in children) Visit(child, active);
            }
            active.Remove(node);
        }
    }
}
