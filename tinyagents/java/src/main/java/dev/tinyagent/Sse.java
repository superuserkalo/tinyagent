package dev.tinyagent;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import tools.jackson.databind.JsonNode;

final class Sse {
    static final int MAX_BYTES = 1_048_576;

    @FunctionalInterface
    interface Parser {
        /** Null means the event did not finish the turn. */
        ProviderTurn accept(JsonNode event, Provider.TextSink text) throws Exception;
    }

    private Sse() {}

    static ProviderTurn read(
            InputStream source, Parser parser, CancellationToken cancellation, Provider.TextSink text)
            throws Exception {
        var input = new BufferedInputStream(source);
        var data = new StringBuilder();
        int bytes = 0;
        boolean hasData = false;
        while (true) {
            cancellation.check();
            String line = line(input, cancellation);
            if (line == null || line.isEmpty()) {
                if (hasData) {
                    if (data.toString().equals("[DONE]")) {
                        throw new ProviderException("Provider stream ended without a complete turn");
                    }
                    var turn = parser.accept(Json.parse(data.toString()), text);
                    cancellation.check();
                    if (turn != null) return turn;
                }
                data.setLength(0);
                bytes = 0;
                hasData = false;
                if (line == null) throw new ProviderException("Provider stream ended without a complete turn");
            } else if (line.equals("data") || line.startsWith("data:")) {
                String value = line.equals("data") ? "" : line.substring(5);
                if (value.startsWith(" ")) value = value.substring(1);
                bytes += value.getBytes(StandardCharsets.UTF_8).length + (hasData ? 1 : 0);
                if (bytes > MAX_BYTES) throw new ProviderException("SSE event exceeds its byte limit");
                if (hasData) data.append('\n');
                data.append(value);
                hasData = true;
            }
        }
    }

    private static String line(InputStream input, CancellationToken cancellation) throws IOException {
        var bytes = new ByteArrayOutputStream();
        while (true) {
            cancellation.check();
            int value = input.read();
            if (value == -1 && bytes.size() == 0) return null;
            if (value == '\n' || value == -1) break;
            if (bytes.size() == MAX_BYTES) throw new ProviderException("SSE line exceeds its byte limit");
            bytes.write(value);
        }
        byte[] data = bytes.toByteArray();
        int length = data.length;
        if (length > 0 && data[length - 1] == '\r') length--;
        try {
            return StandardCharsets.UTF_8.newDecoder()
                    .onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT)
                    .decode(ByteBuffer.wrap(data, 0, length))
                    .toString();
        } catch (CharacterCodingException error) {
            throw new ProviderException("Provider stream is not valid UTF-8", error);
        }
    }
}
