package dev.tinyagent;

import java.util.Objects;
import tools.jackson.databind.JsonNode;

/** Immutable provider metadata. Tool adds schema validation and a handler. */
public record ToolDefinition(String name, String description, JsonNode parameters) {
    public ToolDefinition {
        Json.text(name, "tool name");
        Json.text(description, "tool description");
        parameters = Objects.requireNonNull(parameters).deepCopy();
    }

    @Override
    public JsonNode parameters() {
        return parameters.deepCopy();
    }
}
