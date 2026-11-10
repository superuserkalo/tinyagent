package dev.tinyagent;

import java.io.IOException;
import java.io.InputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import tools.jackson.databind.node.ObjectNode;

abstract class HttpProvider implements Provider {
    private static final ScheduledThreadPoolExecutor DEADLINES = deadlines();
    private final HttpClient client;
    private final boolean ownsClient;
    private final URI endpoint;
    private final String key;
    private final String keyEnvironment;
    private final Duration timeout;

    HttpProvider(HttpClient client, boolean ownsClient, URI endpoint, String key, String keyEnvironment, Duration timeout) {
        if (timeout.isNegative() || timeout.isZero()) throw new IllegalArgumentException("Timeout must be positive");
        this.client = client;
        this.ownsClient = ownsClient;
        this.endpoint = endpoint;
        this.key = key;
        this.keyEnvironment = keyEnvironment;
        this.timeout = timeout;
    }

    abstract ObjectNode requestBody(ProviderRequest request) throws ProviderException;
    abstract Sse.Parser parser();
    abstract void authorize(HttpRequest.Builder request, String key);

    @Override
    @SuppressWarnings("try")
    public final ProviderTurn stream(ProviderRequest request, CancellationToken cancellation, TextSink text)
            throws Exception {
        cancellation.check();
        String apiKey = key == null ? System.getenv(keyEnvironment) : key;
        if (apiKey == null || apiKey.isBlank()) throw new ProviderException(keyEnvironment + " is not set");
        var http = HttpRequest.newBuilder(endpoint)
                .timeout(timeout)
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofByteArray(Json.MAPPER.writeValueAsBytes(requestBody(request))));
        authorize(http, apiKey);
        try (var scope = new RequestScope(cancellation, timeout)) {
            try {
                scope.cancellation.check();
                var pending = client.sendAsync(http.build(), HttpResponse.BodyHandlers.ofInputStream());
                HttpResponse<InputStream> response;
                try (var cancelRequest = scope.cancellation.onCancel(() -> pending.cancel(true))) {
                    try {
                        response = pending.get();
                    } catch (InterruptedException error) {
                        pending.cancel(true);
                        throw error;
                    } catch (ExecutionException error) {
                        throw new ProviderException("Provider request failed", error.getCause());
                    }
                }
                try (var input = response.body();
                     var cancelBody = scope.cancellation.onCancel(() -> closeBody(input))) {
                    scope.cancellation.check();
                    if (response.statusCode() < 200 || response.statusCode() >= 300) {
                        String body = new String(input.readNBytes(8192), StandardCharsets.UTF_8);
                        throw new ProviderException("Provider HTTP " + response.statusCode() + ": " + body);
                    }
                    return Sse.read(input, parser(), scope.cancellation, text);
                }
            } catch (InterruptedException error) {
                scope.cancellation.cancel();
                Thread.currentThread().interrupt();
                throw error;
            } catch (Exception error) {
                if (scope.expired.get()) throw new ProviderException("Provider request timed out", error);
                scope.cancellation.check();
                throw error;
            }
        }
    }

    @Override
    public final void close() {
        if (ownsClient) client.shutdownNow();
    }

    private static void closeBody(InputStream input) {
        try {
            input.close();
        } catch (IOException ignored) {
            // Closing still releases the pending read or reports its own error.
        }
    }

    private static ScheduledThreadPoolExecutor deadlines() {
        var executor = new ScheduledThreadPoolExecutor(
                1, Thread.ofPlatform().daemon().name("tinyagent-request-deadline").factory());
        executor.setRemoveOnCancelPolicy(true);
        return executor;
    }

    /** HttpRequest's timer can end at headers for InputStream bodies. This
     * deadline remains armed until parsing and body cleanup finish. */
    private static final class RequestScope implements AutoCloseable {
        final CancellationToken cancellation = new CancellationToken();
        final AtomicBoolean expired = new AtomicBoolean();
        private final CancellationToken.Registration parent;
        private final ScheduledFuture<?> deadline;

        RequestScope(CancellationToken parent, Duration timeout) {
            this.parent = parent.onCancel(cancellation::cancel);
            deadline = DEADLINES.schedule(() -> {
                expired.set(true);
                cancellation.cancel();
            }, timeout.toNanos(), TimeUnit.NANOSECONDS);
        }

        @Override
        public void close() {
            deadline.cancel(false);
            parent.close();
            cancellation.cancel();
        }
    }
}
