package dev.tinyagent;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.util.ArrayDeque;
import java.util.Arrays;
import java.util.concurrent.CountDownLatch;
import tools.jackson.databind.node.ObjectNode;

/** Bounded serialized lines. A run cannot finish before its final line is written. */
final class RpcOutput {
    static final class Line {
        final ByteBuffer bytes;
        final int size;
        final CancellationToken token;
        final CountDownLatch delivered = new CountDownLatch(1);
        volatile IOException error;

        Line(byte[] bytes, CancellationToken token, IOException error) {
            this.bytes = ByteBuffer.wrap(bytes);
            this.size = bytes.length;
            this.token = token;
            this.error = error;
        }
    }

    private final ArrayDeque<Line> lines = new ArrayDeque<>();
    private Line current;
    private int queuedBytes;
    private IOException failure;
    private boolean finished;

    void emit(ObjectNode value, CancellationToken token) throws IOException, InterruptedException {
        byte[] bytes = Json.MAPPER.writeValueAsBytes(value);
        RpcException error = null;
        if (bytes.length > RpcProtocol.MAX_LINE_BYTES) {
            error = new RpcException("output_too_large", "RPC output exceeds " + RpcProtocol.MAX_LINE_BYTES + " UTF-8 bytes");
            bytes = errorBytes(error);
        } else {
            bytes = Arrays.copyOf(bytes, bytes.length + 1);
            bytes[bytes.length - 1] = '\n';
        }
        var line = new Line(bytes, token, error);
        synchronized (this) {
            if (failure != null) throw failure;
            if (finished) throw new IOException("RPC output is closed");
            if (queuedBytes + bytes.length > RpcProtocol.MAX_QUEUE_BYTES) {
                var overflow = new RpcException("output_queue_full", "RPC output queue exceeds "
                        + RpcProtocol.MAX_QUEUE_BYTES + " UTF-8 bytes");
                failure = overflow;
                finished = true;
                for (var pending : lines) reject(pending, overflow);
                lines.clear();
                if (current != null) reject(current, overflow);
                var last = new Line(errorBytes(overflow), null, null);
                lines.add(last);
                queuedBytes = (current == null ? 0 : current.size) + last.size;
                notifyAll();
                throw overflow;
            }
            lines.add(line);
            queuedBytes += line.size;
            notifyAll();
        }
        if (token != null) line.delivered.await();
        if (line.error != null) throw line.error;
    }

    synchronized Line next() throws InterruptedException {
        while (lines.isEmpty() && !finished) wait();
        if (lines.isEmpty()) return null;
        current = lines.removeFirst();
        return current;
    }

    synchronized void complete(Line line) {
        if (current == line) {
            queuedBytes -= line.size;
            current = null;
            line.delivered.countDown();
        }
    }

    synchronized boolean expire(Line line) {
        if (current != line) return false;
        fail(new RpcException("output_stalled", "Cancelled RPC output did not drain"));
        return true;
    }

    synchronized void finish() {
        finished = true;
        notifyAll();
    }

    synchronized void fail(IOException error) {
        if (failure == null) failure = error;
        if (current != null) reject(current, failure);
        for (var pending : lines) reject(pending, failure);
        lines.clear();
        current = null;
        queuedBytes = 0;
        finished = true;
        notifyAll();
    }

    synchronized IOException failure() { return failure; }

    private static void reject(Line line, IOException error) {
        line.error = error;
        line.delivered.countDown();
    }

    private static byte[] errorBytes(RpcException error) {
        byte[] bytes = Json.MAPPER.writeValueAsBytes(RpcProtocol.error(error.code(), error.getMessage()));
        bytes = Arrays.copyOf(bytes, bytes.length + 1);
        bytes[bytes.length - 1] = '\n';
        return bytes;
    }
}
