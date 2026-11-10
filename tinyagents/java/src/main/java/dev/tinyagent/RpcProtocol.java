package dev.tinyagent;

import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

final class RpcProtocol {
    static final int MAX_LINE_BYTES = 1_048_576;
    static final int MAX_QUEUE_BYTES = 4_194_304;

    private RpcProtocol() {}

    static ObjectNode error(String code, String message) {
        var value = Json.object("protocol_error");
        value.putObject("error").put("code", code).put("message", message);
        return value;
    }

    static void validateText(JsonNode value) {
        if (value.isString()) Json.text(value.stringValue(), "JSON string");
        for (String key : value.propertyNames()) Json.text(key, "JSON property");
        for (var child : value) validateText(child);
    }
}
