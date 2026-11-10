package dev.tinyagent;

import tools.jackson.databind.node.ObjectNode;

/** Immutable run events. Each JSON conversion returns a fresh protocol object. */
public sealed interface AgentEvent {
    String runId();
    long sequence();

    record Start(String runId, long sequence) implements AgentEvent {
        public Start { validate(runId, sequence); }
    }

    record TextDelta(String runId, long sequence, String delta) implements AgentEvent {
        public TextDelta {
            validate(runId, sequence);
            Json.text(delta, "delta");
        }
    }

    record Completed(String runId, long sequence, String text) implements AgentEvent {
        public Completed {
            validate(runId, sequence);
            Json.text(text, "text");
        }
    }

    record ToolStarted(String runId, long sequence, String toolCallId, String toolName, ObjectNode arguments) implements AgentEvent {
        public ToolStarted {
            validate(runId, sequence);
            Json.text(toolCallId, "toolCallId");
            Json.text(toolName, "toolName");
            arguments = arguments.deepCopy();
        }

        @Override
        public ObjectNode arguments() { return arguments.deepCopy(); }
    }

    record ToolEnded(String runId, long sequence, String toolCallId, String toolName, String output, boolean isError) implements AgentEvent {
        public ToolEnded {
            validate(runId, sequence);
            Json.text(toolCallId, "toolCallId");
            Json.text(toolName, "toolName");
            Json.text(output, "tool output");
        }
    }

    record Failed(String runId, long sequence, String message) implements AgentEvent {
        public Failed {
            validate(runId, sequence);
            Json.text(message, "message");
        }
    }

    record Aborted(String runId, long sequence) implements AgentEvent {
        public Aborted { validate(runId, sequence); }
    }

    default ObjectNode toJson() {
        var value = Json.MAPPER.createObjectNode().put("runId", runId()).put("sequence", sequence());
        switch (this) {
            case Start ignored -> value.put("type", "agent_start");
            case TextDelta event -> value.put("type", "text_delta").put("delta", event.delta());
            case ToolStarted event -> value.put("type", "tool_execution_start")
                    .put("toolCallId", event.toolCallId()).put("toolName", event.toolName()).set("arguments", event.arguments());
            case ToolEnded event -> value.put("type", "tool_execution_end")
                    .put("toolCallId", event.toolCallId()).put("toolName", event.toolName())
                    .put("output", event.output()).put("isError", event.isError());
            case Completed event -> value.put("type", "agent_end").put("status", "completed").put("text", event.text());
            case Failed event -> {
                value.put("type", "agent_end").put("status", "failed");
                value.putObject("error").put("code", "provider_error").put("message", event.message()).put("retryable", false);
            }
            case Aborted ignored -> value.put("type", "agent_end").put("status", "aborted");
        }
        return value;
    }

    private static void validate(String runId, long sequence) {
        Json.text(runId, "runId");
        if (runId.isEmpty()) throw new IllegalArgumentException("runId must be nonempty");
        if (sequence < 1) throw new IllegalArgumentException("sequence must be positive");
    }
}
