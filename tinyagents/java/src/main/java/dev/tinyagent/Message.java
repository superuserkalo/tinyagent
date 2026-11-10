package dev.tinyagent;

import java.util.List;
import java.util.Objects;
import tools.jackson.databind.JsonNode;

/** Immutable neutral history. Replay stores opaque provider-specific blocks. */
public sealed interface Message {
    record User(String text) implements Message {
        public User { Objects.requireNonNull(text); }
    }

    record Assistant(String text, List<ToolCall> toolCalls, List<JsonNode> replay) implements Message {
        public Assistant {
            Objects.requireNonNull(text);
            toolCalls = List.copyOf(toolCalls);
            replay = Json.copy(replay);
        }

        @Override
        public List<JsonNode> replay() {
            return Json.copy(replay);
        }
    }

    record ToolResult(String callId, String name, String output, boolean isError) implements Message {
        public ToolResult {
            Objects.requireNonNull(callId);
            Objects.requireNonNull(name);
            Objects.requireNonNull(output);
        }
    }
}
