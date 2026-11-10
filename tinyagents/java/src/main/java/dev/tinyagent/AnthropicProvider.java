package dev.tinyagent;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.TreeMap;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

public final class AnthropicProvider extends HttpProvider {
    private final int maxTokens;

    public AnthropicProvider() {
        super(HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(30)).build(), true,
                URI.create("https://api.anthropic.com/v1/messages"), null, "ANTHROPIC_API_KEY", Duration.ofSeconds(180));
        maxTokens = 8192;
    }

    /** The caller retains ownership of an injected HTTP client. */
    public AnthropicProvider(HttpClient client, URI endpoint, String apiKey, Duration timeout, int maxTokens) {
        super(client, false, endpoint, apiKey, "ANTHROPIC_API_KEY", timeout);
        if (maxTokens <= 0) throw new IllegalArgumentException("maxTokens must be positive");
        this.maxTokens = maxTokens;
    }

    @Override
    void authorize(HttpRequest.Builder request, String key) {
        request.header("x-api-key", key).header("anthropic-version", "2023-06-01").header("Accept", "text/event-stream");
    }

    @Override
    ObjectNode requestBody(ProviderRequest request) throws ProviderException {
        var body = Json.MAPPER.createObjectNode().put("model", request.model()).put("system", request.instructions())
                .put("max_tokens", maxTokens).put("stream", true);
        var messages = body.putArray("messages");
        ArrayNode previousResults = null;
        for (var message : request.history()) {
            if (message instanceof Message.ToolResult result) {
                var block = Json.object("tool_result").put("tool_use_id", result.callId())
                        .put("content", result.output()).put("is_error", result.isError());
                if (previousResults == null) {
                    var user = Json.MAPPER.createObjectNode().put("role", "user");
                    previousResults = user.putArray("content");
                    messages.add(user);
                }
                previousResults.add(block);
                continue;
            }
            previousResults = null;
            switch (message) {
                case Message.User user -> {
                    var value = Json.MAPPER.createObjectNode().put("role", "user");
                    value.putArray("content").add(Json.object("text").put("text", user.text()));
                    messages.add(value);
                }
                case Message.Assistant assistant -> {
                    var value = Json.MAPPER.createObjectNode().put("role", "assistant");
                    var content = value.putArray("content");
                    var replay = assistant.replay();
                    if (!replay.isEmpty()) {
                        if (replay.size() != 1 || !replay.getFirst().path("content").isArray()) {
                            throw new ProviderException("Anthropic replay must contain one complete message");
                        }
                        replay.getFirst().path("content").forEach(content::add);
                    } else {
                        if (!assistant.text().isEmpty()) content.add(Json.object("text").put("text", assistant.text()));
                        for (var call : assistant.toolCalls()) {
                            content.add(Json.object("tool_use").put("id", call.callId()).put("name", call.name()).set("input", call.arguments()));
                        }
                    }
                    messages.add(value);
                }
                case Message.ToolResult ignored -> throw new IllegalStateException("Tool result already handled");
            }
        }
        var tools = body.putArray("tools");
        for (var tool : request.tools()) {
            tools.add(Json.MAPPER.createObjectNode().put("name", tool.name()).put("description", tool.description())
                    .set("input_schema", tool.parameters()));
        }
        return body;
    }

    @Override
    Sse.Parser parser() {
        return new Response();
    }

    private static final class Block {
        final ObjectNode value;
        final Arguments arguments = new Arguments();
        boolean open = true;

        Block(ObjectNode value) {
            this.value = value.deepCopy();
        }
    }

    private static final class Response implements Sse.Parser {
        private ObjectNode message;
        private final ObjectNode usage = Json.MAPPER.createObjectNode();
        private final TreeMap<Integer, Block> blocks = new TreeMap<>();

        @Override
        public ProviderTurn accept(JsonNode event, TextSink text) throws Exception {
            switch (Json.requiredString(event, "type")) {
                case "message_start" -> {
                    if (message != null) throw new ProviderException("Repeated Anthropic message_start");
                    message = Json.requireObject(event.get("message"), "message").deepCopy();
                    if (message.has("usage")) usage.setAll(Json.requireObject(message.get("usage"), "usage"));
                }
                case "content_block_start" -> {
                    int index = Json.index(event.path("index"));
                    if (message == null || blocks.containsKey(index)) throw new ProviderException("Invalid Anthropic block order");
                    var block = Json.requireObject(event.get("content_block"), "content block");
                    Json.requiredString(block, "type");
                    blocks.put(index, new Block(block));
                }
                case "content_block_delta" -> {
                    var block = openBlock(event);
                    var delta = Json.requireObject(event.get("delta"), "block delta");
                    String kind = Json.requiredString(delta, "type");
                    String field;
                    String expected;
                    switch (kind) {
                        case "text_delta" -> { field = "text"; expected = "text"; }
                        case "thinking_delta" -> { field = "thinking"; expected = "thinking"; }
                        case "signature_delta" -> { field = "signature"; expected = "thinking"; }
                        case "input_json_delta" -> { field = "partial_json"; expected = "tool_use"; }
                        default -> { return null; }
                    }
                    if (!Json.requiredString(block.value, "type").equals(expected)) {
                        throw new ProviderException("Anthropic delta does not match its block");
                    }
                    String value = Json.string(delta, field);
                    if (kind.equals("input_json_delta")) {
                        block.arguments.append(value);
                    } else {
                        String previous = block.value.has(field) ? Json.string(block.value, field) : "";
                        block.value.put(field, previous + value);
                        if (kind.equals("text_delta")) text.accept(value);
                    }
                }
                case "content_block_stop" -> {
                    var block = openBlock(event);
                    block.open = false;
                    if (Json.requiredString(block.value, "type").equals("tool_use")) {
                        if (!block.arguments.value().isEmpty()) block.value.set("input", Json.parse(block.arguments.value()));
                        if (!block.value.path("input").isObject()) throw new ProviderException("Anthropic tool arguments must be an object");
                    }
                }
                case "message_delta" -> {
                    if (message == null) throw new ProviderException("Anthropic delta preceded message_start");
                    message.setAll(Json.requireObject(event.get("delta"), "message delta"));
                    if (event.has("usage")) usage.setAll(Json.requireObject(event.get("usage"), "usage delta"));
                }
                case "message_stop" -> { return complete(); }
                case "error" -> throw new ProviderException("Anthropic stream failed: " + event);
                default -> {}
            }
            return null;
        }

        private Block openBlock(JsonNode event) throws ProviderException {
            var block = blocks.get(Json.index(event.path("index")));
            if (block == null || !block.open) throw new ProviderException("Anthropic event referenced an unopened block");
            return block;
        }

        private ProviderTurn complete() throws ProviderException {
            if (message == null) throw new ProviderException("Anthropic stream stopped without a message");
            String reason = Json.requiredString(message, "stop_reason");
            if (!Set.of("end_turn", "tool_use", "stop_sequence", "refusal").contains(reason)) {
                throw new ProviderException("Anthropic response is incomplete: " + reason);
            }
            var output = new ArrayList<JsonNode>();
            var calls = new ArrayList<ToolCall>();
            var ids = new HashSet<String>();
            var text = new StringBuilder();
            for (var block : blocks.values()) {
                if (block.open) throw new ProviderException("Anthropic stream stopped with an unfinished block");
                switch (Json.requiredString(block.value, "type")) {
                    case "text" -> text.append(Json.string(block.value, "text"));
                    case "tool_use" -> {
                        String id = Json.requiredString(block.value, "id");
                        String name = Json.requiredString(block.value, "name");
                        if (!ids.add(id)) throw new ProviderException("Duplicate Anthropic tool call");
                        calls.add(new ToolCall(id, name, block.value.get("input")));
                    }
                    default -> {}
                }
                output.add(block.value);
            }
            var replay = message.deepCopy();
            var content = replay.putArray("content");
            output.forEach(content::add);
            replay.set("usage", usage);
            return new ProviderTurn(text.toString(), calls, List.of(replay), output);
        }
    }
}
