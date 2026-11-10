package dev.tinyagent;

import java.nio.charset.StandardCharsets;

/** Buffers complete tool JSON with a byte bound across all argument deltas. */
final class Arguments {
    private final StringBuilder text = new StringBuilder();
    private int bytes;

    void append(String value) throws ProviderException {
        int added = value.getBytes(StandardCharsets.UTF_8).length;
        if (added > Sse.MAX_BYTES - bytes) throw new ProviderException("Tool arguments exceed their byte limit");
        bytes += added;
        text.append(value);
    }

    void set(String value) throws ProviderException {
        text.setLength(0);
        bytes = 0;
        append(value);
    }

    String value() {
        return text.toString();
    }
}
