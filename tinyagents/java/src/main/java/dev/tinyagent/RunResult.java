package dev.tinyagent;

import java.util.List;
import tools.jackson.databind.JsonNode;

/** Successful final answer and replayable history, isolated from caller mutation. */
public record RunResult(String text, List<Message> history, List<JsonNode> output) {
    public RunResult {
        Json.text(text, "text");
        history = List.copyOf(history);
        output = Json.copy(output);
    }

    @Override
    public List<JsonNode> output() {
        return Json.copy(output);
    }
}
