package dev.tinyagent;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import org.eclipse.jetty.http.HttpHeader;
import org.eclipse.jetty.io.EndPoint;
import org.eclipse.jetty.server.Request;
import org.eclipse.jetty.server.Response;
import org.eclipse.jetty.util.Callback;

/** One HTTP/1 request and one shared session worker; no event queue. */
final class HttpConnection {
    private final Response response;
    private final EndPoint endpoint;
    private final CompletableFuture<Thread> worker = new CompletableFuture<>();
    private CancellationToken cancellation;
    private boolean stopped;
    private boolean started;

    HttpConnection(Request request, Response response) {
        this.response = response;
        endpoint = request.getConnectionMetaData().getConnection().getEndPoint();
        request.addFailureListener(failure -> stop());
    }

    void run(Session session, String message) throws Exception {
        synchronized (this) {
            if (stopped) throw new IOException("HTTP connection closed");
            session.start(message, this::begin, this::event);
            started = true;
        }
        Thread thread = worker.join();
        var discard = ByteBuffer.allocate(8192);
        try {
            while (thread.isAlive()) {
                thread.join(50);
                if (!thread.isAlive()) break;
                // The request body is fully consumed and persistence is disabled.
                // Observe EOF while the provider is silent, discarding pipelined input.
                discard.clear().flip();
                if (endpoint.fill(discard) < 0) stop();
            }
        } catch (IOException failure) {
            stop();
        }
    }

    void stop() {
        CancellationToken token;
        synchronized (this) {
            stopped = true;
            token = cancellation;
        }
        if (token == null) endpoint.close(new IOException("HTTP connection stopped"));
        else token.cancel();
    }

    void finish() {
        synchronized (this) { if (!started) return; }
        try { stop(); }
        finally {
            Thread thread = worker.join();
            boolean interrupted = false;
            while (thread.isAlive()) {
                try { thread.join(); }
                catch (InterruptedException ignored) { interrupted = true; }
            }
            if (interrupted) Thread.currentThread().interrupt();
        }
    }

    private void begin(String runId, CancellationToken token) throws Exception {
        boolean cancel;
        synchronized (this) {
            cancellation = token;
            cancel = stopped;
            worker.complete(Thread.currentThread());
        }
        if (cancel) token.cancel();
        token.check();
        response.setStatus(200);
        response.getHeaders().put(HttpHeader.CONTENT_TYPE, "application/jsonl; charset=utf-8");
        write(new byte[0], false, token);
    }

    private void event(AgentEvent event, CancellationToken token) throws Exception {
        var value = event.toJson();
        byte[] bytes = Json.MAPPER.writeValueAsBytes(value);
        boolean oversized = bytes.length > HttpJson.MAX_EVENT_BYTES;
        if (oversized) {
            value = Json.MAPPER.createObjectNode().put("type", "agent_end")
                    .put("runId", event.runId()).put("sequence", event.sequence()).put("status", "failed");
            value.putObject("error").put("code", "output_too_large")
                    .put("message", "HTTP event exceeds its byte limit").put("retryable", false);
            bytes = Json.MAPPER.writeValueAsBytes(value);
        }
        byte[] line = new byte[bytes.length + 1];
        System.arraycopy(bytes, 0, line, 0, bytes.length);
        line[bytes.length] = '\n';
        boolean last = value.path("type").stringValue().equals("agent_end");
        write(line, last, token);
        if (oversized) throw new IOException("HTTP event exceeded its byte limit");
    }

    private void write(byte[] bytes, boolean last, CancellationToken token) throws Exception {
        var delivered = new CompletableFuture<Void>();
        response.write(last, ByteBuffer.wrap(bytes), Callback.from(delivered));
        long deadline = 0;
        while (true) {
            try {
                delivered.get(50, TimeUnit.MILLISECONDS);
                return;
            } catch (TimeoutException ignored) {
                if (token.isCancelled() && deadline == 0) deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(1);
                if (deadline != 0 && System.nanoTime() >= deadline) {
                    var failure = new IOException("Cancelled HTTP output did not drain");
                    endpoint.close(failure);
                    throw failure;
                }
            } catch (ExecutionException failure) {
                throw new IOException("HTTP delivery failed", failure.getCause());
            }
        }
    }
}
