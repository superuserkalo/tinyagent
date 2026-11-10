package dev.tinyagent;

import com.networknt.schema.Schema;
import com.networknt.schema.SchemaLocation;
import com.networknt.schema.SchemaRegistry;
import com.networknt.schema.SchemaRegistryConfig;
import com.networknt.schema.dialect.Dialects;
import com.networknt.schema.keyword.DynamicRefValidator;
import com.networknt.schema.keyword.RefValidator;
import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import tools.jackson.databind.DeserializationFeature;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/** NetworkNT owns keyword validation; this boundary fixes the dialect and reference policy. */
final class ToolSchema {
    private static final String DIALECT = "https://json-schema.org/draft/2020-12/schema";
    private static final JsonMapper SCHEMA_JSON = JsonMapper.builder()
            .enable(DeserializationFeature.USE_BIG_INTEGER_FOR_INTS, DeserializationFeature.USE_BIG_DECIMAL_FOR_FLOATS).build();
    private static final SchemaRegistryConfig CONFIG = SchemaRegistryConfig.builder()
            .formatAssertionsEnabled(false).typeLoose(false).preloadSchema(false).build();
    private static final Schema META = SchemaRegistry.withDialect(Dialects.getDraft202012(),
            builder -> builder.schemaRegistryConfig(CONFIG)).getSchema(SchemaLocation.of(DIALECT));
    private final Schema schema;

    ToolSchema(JsonNode parameters) {
        if (!META.validate(parameters).isEmpty()) {
            throw new IllegalArgumentException("Tool parameters must be a valid Draft 2020-12 schema");
        }
        var registry = SchemaRegistry.withDialect(Dialects.getDraft202012(), builder -> builder
                .schemaRegistryConfig(CONFIG).schemaLoader(loader -> loader.block(iri -> true)));
        // Arbitrary-precision bounds avoid NetworkNT's long fast path for large exponent inputs.
        // Provider metadata and handler arguments keep their original JSON nodes.
        schema = registry.getSchema(SCHEMA_JSON.readTree(parameters.toString()));
        var nodes = new IdentityHashMap<JsonNode, Schema>();
        var edges = new IdentityHashMap<JsonNode, List<JsonNode>>();
        visit(schema, nodes, edges);
        var pending = new ArrayList<>(nodes.values());
        for (int index = 0; index < pending.size(); index++) {
            var current = pending.get(index);
            for (var validator : current.getValidators()) {
                Schema target = switch (validator) {
                    case RefValidator reference -> reference.getSchemaRef().getSchema();
                    case DynamicRefValidator reference -> reference.getSchemaRef(current.createExecutionContext()).getSchema();
                    default -> null;
                };
                if (validator instanceof RefValidator || validator instanceof DynamicRefValidator) {
                    if (target == null) throw new IllegalArgumentException("Tool schema reference cannot be resolved locally");
                    if (!nodes.containsKey(target.getSchemaNode()) && target.getSchemaNode().isObject()) {
                        var added = new IdentityHashMap<JsonNode, Schema>();
                        visit(target, added, edges);
                        for (var entry : added.entrySet()) {
                            if (nodes.putIfAbsent(entry.getKey(), entry.getValue()) == null) pending.add(entry.getValue());
                        }
                    }
                    if (target.getSchemaNode().isObject()) edges.get(current.getSchemaNode()).add(target.getSchemaNode());
                }
            }
        }
        Set<JsonNode> active = Collections.newSetFromMap(new IdentityHashMap<>());
        Set<JsonNode> finished = Collections.newSetFromMap(new IdentityHashMap<>());
        for (var node : nodes.keySet()) checkCycle(node, edges, active, finished);
        schema.initializeValidators();
    }

    boolean valid(ObjectNode arguments) { return schema.validate(arguments).isEmpty(); }

    private static void visit(Schema schema, Map<JsonNode, Schema> nodes, Map<JsonNode, List<JsonNode>> edges) {
        var node = schema.getSchemaNode();
        if (!node.isObject() || nodes.putIfAbsent(node, schema) != null) return;
        if (node.has("$schema") && !DIALECT.equals(node.get("$schema").stringValue())) {
            throw new IllegalArgumentException("Tool schemas must use Draft 2020-12");
        }
        edges.putIfAbsent(node, new ArrayList<>());
        for (var property : node.properties()) {
            var name = property.getKey();
            var value = property.getValue();
            var location = schema.getSchemaLocation().append(name);
            switch (name) {
                case "$defs", "definitions", "properties", "patternProperties", "dependentSchemas" -> {
                    for (var child : value.properties()) {
                        child(schema, child.getValue(), location.append(child.getKey()),
                                name.equals("dependentSchemas"), nodes, edges);
                    }
                }
                case "allOf", "anyOf", "oneOf", "prefixItems" -> {
                    for (int index = 0; index < value.size(); index++) {
                        child(schema, value.get(index), location.append(index), !name.equals("prefixItems"), nodes, edges);
                    }
                }
                case "not", "if", "then", "else", "items", "contains", "additionalProperties",
                        "unevaluatedProperties", "unevaluatedItems", "propertyNames", "contentSchema" ->
                    child(schema, value, location, Set.of("not", "if", "then", "else").contains(name), nodes, edges);
                default -> { }
            }
        }
    }

    private static void child(Schema parent, JsonNode value, SchemaLocation location, boolean sameInput,
            Map<JsonNode, Schema> nodes, Map<JsonNode, List<JsonNode>> edges) {
        if (!value.isObject()) return;
        var schema = parent.getSchemaContext().newSchema(location, value, parent);
        visit(schema, nodes, edges);
        if (sameInput) edges.get(parent.getSchemaNode()).add(value);
    }

    private static void checkCycle(JsonNode node, Map<JsonNode, List<JsonNode>> edges,
            Set<JsonNode> active, Set<JsonNode> finished) {
        if (finished.contains(node)) return;
        if (!active.add(node)) throw new IllegalArgumentException("Tool schema reference cycle does not descend into its input");
        for (var child : edges.get(node)) checkCycle(child, edges, active, finished);
        active.remove(node);
        finished.add(node);
    }
}
