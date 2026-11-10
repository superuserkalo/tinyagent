package dev.tinyagent;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import org.eclipse.jetty.http.HttpHeader;
import org.eclipse.jetty.server.Request;
import org.eclipse.jetty.server.Response;
import org.eclipse.jetty.util.Callback;
import tools.jackson.databind.node.ObjectNode;

/** Bounded request bodies and JSON errors; Jetty owns HTTP framing. */
final class HttpJson {
    static final int MAX_BODY_BYTES = 1_048_576;
    static final int MAX_EVENT_BYTES = 1_048_576;

    static final class Failure extends IOException {
        private static final long serialVersionUID = 1L;
        final int status;
        final String code;

        Failure(int status, String code, String message) {
            super(message);
            this.status = status;
            this.code = code;
        }
    }

    private HttpJson() {}

    static ObjectNode read(Request request) throws IOException {
        if (request.getLength() > MAX_BODY_BYTES) {
            throw new Failure(413, "request_too_large", "HTTP body exceeds its byte limit");
        }
        if (request.getLength() == 0 || (request.getLength() < 0 &&
                !request.getHeaders().contains(HttpHeader.TRANSFER_ENCODING))) {
            return Json.MAPPER.createObjectNode();
        }
        var types = request.getHeaders().getValuesList(HttpHeader.CONTENT_TYPE);
        if (types.size() != 1 || !types.getFirst().split(";", 2)[0].strip()
                .toLowerCase(Locale.ROOT).equals("application/json")) {
            throw new Failure(415, "unsupported_media_type", "Content-Type must be application/json");
        }
        byte[] bytes = Request.asInputStream(request).readNBytes(MAX_BODY_BYTES + 1);
        if (bytes.length > MAX_BODY_BYTES) {
            throw new Failure(413, "request_too_large", "HTTP body exceeds its byte limit");
        }
        try {
            var body = StandardCharsets.UTF_8.newDecoder().decode(ByteBuffer.wrap(bytes)).toString();
            var value = Json.requireObject(Json.parse(body), "body");
            RpcProtocol.validateText(value);
            return value;
        } catch (CharacterCodingException | ProviderException | IllegalArgumentException failure) {
            throw new Failure(400, "invalid_request", "Body must be a UTF-8 JSON object");
        }
    }

    static void write(Response response, int status, ObjectNode value, Callback callback) {
        response.setStatus(status);
        response.getHeaders().put(HttpHeader.CONTENT_TYPE, "application/json; charset=utf-8");
        byte[] bytes = value == null ? new byte[0] : Json.MAPPER.writeValueAsBytes(value);
        response.write(true, ByteBuffer.wrap(bytes), callback);
    }

    static void error(Response response, int status, String code, String message, Callback callback) {
        var value = Json.MAPPER.createObjectNode();
        value.putObject("error").put("code", code).put("message", message).put("retryable", status == 409);
        if (status == 401) response.getHeaders().put(HttpHeader.WWW_AUTHENTICATE, "Bearer");
        write(response, status, value, callback);
    }
}
