package dev.tinyagent;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.TreeMap;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

public final class OpenAIProvider extends HttpProvider {
    public OpenAIProvider() {
        super(HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(30)).build(), true,
                URI.create("https://api.openai.com/v1/responses"), null, "OPENAI_API_KEY", Duration.ofSeconds(180));
    }

    /** The caller retains ownership of an injected HTTP client. */
    public OpenAIProvider(HttpClient client, URI endpoint, String apiKey, Duration timeout) {
        super(client, false, endpoint, apiKey, "OPENAI_API_KEY", timeout);
    }

    @Override
    void authorize(HttpRequest.Builder request, String key) {
        request.header("Authorization", "Bearer " + key).header("Accept", "text/event-stream");
    }

    @Override
    ObjectNode requestBody(ProviderRequest request) {
        var body = Json.MAPPER.createObjectNode()
                .put("model", request.model()).put("instructions", request.instructions())
                .put("parallel_tool_calls", false).put("store", false).put("stream", true);
        body.putArray("include").add("reasoning.encrypted_content");
        var input = body.putArray("input");
        for (var message : request.history()) {
            switch (message) {
                case Message.User user ->
                        input.add(Json.MAPPER.createObjectNode().put("role", "user").put("content", user.text()));
                case Message.Assistant assistant -> {
                    var replay = assistant.replay();
                    if (!replay.isEmpty()) {
                        replay.forEach(input::add);
                    } else {
                        if (!assistant.text().isEmpty()) {
                            input.add(Json.MAPPER.createObjectNode().put("role", "assistant").put("content", assistant.text()));
                        }
                        for (var call : assistant.toolCalls()) {
                            input.add(Json.object("function_call").put("call_id", call.callId()).put("name", call.name())
                                    .put("arguments", call.arguments().toString()));
                        }
                    }
                }
                case Message.ToolResult result ->
                        input.add(Json.object("function_call_output").put("call_id", result.callId()).put("output", result.output()));
            }
        }
        var tools = body.putArray("tools");
        for (var tool : request.tools()) {
            tools.add(Json.object("function").put("name", tool.name()).put("description", tool.description())
                    .put("strict", true).set("parameters", tool.parameters()));
        }
        return body;
    }

    @Override
    Sse.Parser parser() {
        return new Response();
    }

    private static final class Response implements Sse.Parser {
        private final TreeMap<Integer, ObjectNode> items = new TreeMap<>();
        private final TreeMap<Integer, Arguments> arguments = new TreeMap<>();

        @Override
        public ProviderTurn accept(JsonNode event, TextSink text) throws Exception {
            switch (Json.requiredString(event, "type")) {
                case "response.output_item.added", "response.output_item.done" -> {
                    int index = Json.index(event.path("output_index"));
                    var item = Json.requireObject(event.get("item"), "output item").deepCopy();
                    if (Json.requiredString(item, "type").equals("function_call")) {
                        String value = Json.string(item, "arguments");
                        if (!value.isEmpty()) arguments.computeIfAbsent(index, ignored -> new Arguments()).set(value);
                    }
                    items.put(index, item);
                }
                case "response.output_text.delta" -> text.accept(Json.string(event, "delta"));
                case "response.function_call_arguments.delta" ->
                        arguments.computeIfAbsent(Json.index(event.path("output_index")), ignored -> new Arguments())
                                .append(Json.string(event, "delta"));
                case "response.function_call_arguments.done" ->
                        arguments.computeIfAbsent(Json.index(event.path("output_index")), ignored -> new Arguments())
                                .set(Json.string(event, "arguments"));
                case "response.completed" -> { return complete(event); }
                case "response.failed", "response.incomplete", "error" ->
                        throw new ProviderException("OpenAI stream failed: " + event);
                default -> {}
            }
            return null;
        }

        private ProviderTurn complete(JsonNode event) throws ProviderException {
            var response = Json.requireObject(event.get("response"), "completed response");
            if (response.has("status") && !Json.string(response, "status").equals("completed")) {
                throw new ProviderException("OpenAI response is incomplete");
            }
            var finalItems = new TreeMap<>(items);
            if (response.has("output")) {
                var output = response.get("output");
                if (!output.isArray()) throw new ProviderException("OpenAI output must be an array");
                finalItems.clear();
                for (int index = 0; index < output.size(); index++) {
                    finalItems.put(index, Json.requireObject(output.get(index), "output item").deepCopy());
                }
            }
            var output = new ArrayList<JsonNode>();
            var calls = new ArrayList<ToolCall>();
            var ids = new HashSet<String>();
            var text = new StringBuilder();
            for (var entry : finalItems.entrySet()) {
                var item = entry.getValue();
                switch (Json.requiredString(item, "type")) {
                    case "function_call" -> {
                        String raw = item.has("arguments") ? Json.string(item, "arguments") : "";
                        if (raw.isEmpty() && arguments.containsKey(entry.getKey())) raw = arguments.get(entry.getKey()).value();
                        if (raw.getBytes(StandardCharsets.UTF_8).length > Sse.MAX_BYTES) {
                            throw new ProviderException("Tool arguments exceed their byte limit");
                        }
                        var value = Json.parse(raw);
                        String id = Json.requiredString(item, "call_id");
                        String name = Json.requiredString(item, "name");
                        if (!value.isObject() || !ids.add(id)) throw new ProviderException("Invalid OpenAI function call");
                        item.put("arguments", raw);
                        calls.add(new ToolCall(id, name, value));
                    }
                    case "message" -> {
                        var content = item.path("content");
                        if (!content.isArray()) throw new ProviderException("OpenAI message is missing content");
                        for (var part : content) {
                            if (Json.requiredString(part, "type").equals("output_text")) text.append(Json.string(part, "text"));
                        }
                    }
                    default -> {}
                }
                output.add(item);
            }
            return new ProviderTurn(text.toString(), calls, output, output);
        }
    }
}
