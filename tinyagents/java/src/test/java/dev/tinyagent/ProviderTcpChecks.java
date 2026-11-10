package dev.tinyagent;

import com.sun.net.httpserver.HttpServer;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.CancellationException;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicReference;

/** These checks deliberately fail when real local TCP binding is unavailable. */
final class ProviderTcpChecks {
    private ProviderTcpChecks() {}

    static void run(boolean anthropic) throws Exception {
        var server = HttpServer.create(new InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0);
        var hold = new CountDownLatch(1);
        var bodyStarted = new CountDownLatch(1);
        try (var executor = Executors.newVirtualThreadPerTaskExecutor();
             var client = HttpClient.newHttpClient()) {
            try {
                server.setExecutor(executor);
                server.createContext("/complete", exchange -> {
                    exchange.getRequestBody().readAllBytes();
                    exchange.getResponseHeaders().set("Content-Type", "text/event-stream");
                    exchange.sendResponseHeaders(200, 0);
                    try (exchange) {
                        exchange.getResponseBody().write(ProviderChecks.fixture(anthropic, false));
                        exchange.getResponseBody().flush();
                        try { hold.await(); }
                        catch (InterruptedException error) { Thread.currentThread().interrupt(); }
                    }
                });
                server.createContext("/blocked", exchange -> {
                    exchange.getRequestBody().readAllBytes();
                    exchange.sendResponseHeaders(200, 0);
                    try (exchange) {
                        exchange.getResponseBody().write(": waiting\n\n".getBytes(StandardCharsets.UTF_8));
                        exchange.getResponseBody().flush();
                        bodyStarted.countDown();
                        try { hold.await(); }
                        catch (InterruptedException error) { Thread.currentThread().interrupt(); }
                    }
                });
                server.start();
                var address = server.getAddress();
                var base = new URI("http", null, address.getAddress().getHostAddress(),
                        address.getPort(), "/", null, null);
                Provider complete = provider(anthropic, client, base.resolve("/complete"));
                var turn = complete.stream(ProviderChecks.request(List.of()), new CancellationToken(), delta -> {});
                ProviderChecks.equal(turn.text(), "done\ncafé");
                var token = new CancellationToken();
                var failure = new AtomicReference<Throwable>();
                var worker = ProviderChecks.worker(() -> provider(anthropic, client, base.resolve("/blocked"))
                        .stream(ProviderChecks.request(List.of()), token, delta -> {}), failure);
                ProviderChecks.await(bodyStarted);
                token.cancel();
                ProviderChecks.join(worker);
                ProviderChecks.truth(failure.get() instanceof CancellationException, "Real body read was not cancelled");
            } finally {
                hold.countDown();
                server.stop(0);
            }
        }
    }

    private static Provider provider(boolean anthropic, HttpClient client, URI endpoint) {
        return anthropic
                ? new AnthropicProvider(client, endpoint, "fixture-key", Duration.ofSeconds(3), 1024)
                : new OpenAIProvider(client, endpoint, "fixture-key", Duration.ofSeconds(3));
    }
}
