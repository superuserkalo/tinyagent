package dev.tinyagent;

import java.util.Objects;
import tools.jackson.databind.JsonNode;

/** Complete model arguments; execution must validate them against the tool schema. */
public record ToolCall(String callId, String name, JsonNode arguments) {
    public ToolCall {
        Objects.requireNonNull(callId);
        Objects.requireNonNull(name);
        arguments = Objects.requireNonNull(arguments).deepCopy();
    }

    @Override
    public JsonNode arguments() {
        return arguments.deepCopy();
    }
}
