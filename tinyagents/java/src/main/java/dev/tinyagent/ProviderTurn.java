package dev.tinyagent;

import java.util.List;
import java.util.Objects;
import tools.jackson.databind.JsonNode;

public record ProviderTurn(
        String text, List<ToolCall> toolCalls, List<JsonNode> replay, List<JsonNode> output) {
    public ProviderTurn {
        Objects.requireNonNull(text);
        toolCalls = List.copyOf(toolCalls);
        replay = Json.copy(replay);
        output = Json.copy(output);
    }

    @Override
    public List<JsonNode> replay() {
        return Json.copy(replay);
    }

    @Override
    public List<JsonNode> output() {
        return Json.copy(output);
    }
}
