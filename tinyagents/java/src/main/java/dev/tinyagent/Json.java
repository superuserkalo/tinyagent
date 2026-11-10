package dev.tinyagent;

import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Objects;
import tools.jackson.databind.DeserializationFeature;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

final class Json {
    static final JsonMapper MAPPER = JsonMapper.builder()
            .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS)
            .build();

    private Json() {}

    static boolean validText(String value) {
        return StandardCharsets.UTF_8.newEncoder().canEncode(value);
    }

    static String text(String value, String name) {
        Objects.requireNonNull(value, name);
        if (!validText(value)) throw new IllegalArgumentException(name + " contains an unpaired surrogate");
        return value;
    }

    static JsonNode parse(String source) throws ProviderException {
        try {
            var value = MAPPER.readTree(source);
            if (value == null || value.isMissingNode()) throw new IllegalArgumentException("Missing JSON value");
            return value;
        } catch (RuntimeException error) {
            throw new ProviderException("Invalid provider JSON", error);
        }
    }

    static String requiredString(JsonNode value, String field) throws ProviderException {
        String text = string(value, field);
        if (text.isEmpty()) throw new ProviderException("Empty provider field: " + field);
        return text;
    }

    static String string(JsonNode value, String field) throws ProviderException {
        var child = value.path(field);
        if (!child.isString()) {
            throw new ProviderException("Missing or invalid provider field: " + field);
        }
        return child.stringValue();
    }

    static ObjectNode requireObject(JsonNode value, String name) throws ProviderException {
        if (value instanceof ObjectNode object) return object;
        throw new ProviderException("Missing or invalid provider object: " + name);
    }

    static int index(JsonNode value) throws ProviderException {
        if (!value.isIntegralNumber() || !value.canConvertToInt() || value.intValue() < 0) {
            throw new ProviderException("Missing or invalid provider block index");
        }
        return value.intValue();
    }

    static List<JsonNode> copy(List<JsonNode> values) {
        return values.stream().map(JsonNode::deepCopy).toList();
    }

    static ObjectNode object(String type) {
        return MAPPER.createObjectNode().put("type", type);
    }
}
