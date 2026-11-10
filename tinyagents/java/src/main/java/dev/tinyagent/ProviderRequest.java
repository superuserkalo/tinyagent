package dev.tinyagent;

import java.util.List;
import java.util.Objects;

public record ProviderRequest(
        String model, String instructions, List<Message> history, List<ToolDefinition> tools) {
    public ProviderRequest {
        Objects.requireNonNull(model);
        Objects.requireNonNull(instructions);
        history = List.copyOf(history);
        tools = List.copyOf(tools);
    }
}
