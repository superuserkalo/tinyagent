package dev.tinyagent;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.net.Authenticator;
import java.net.CookieHandler;
import java.net.ProxySelector;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpHeaders;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.ByteBuffer;
import java.security.NoSuchAlgorithmException;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executor;
import java.util.concurrent.Flow;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLParameters;
import javax.net.ssl.SSLSession;

/** Uses the JDK's real InputStream body subscriber without binding a socket. */
public class FixtureClient extends HttpClient {
    static final HttpHeaders HEADERS = HttpHeaders.of(Map.of("Content-Type", List.of("text/event-stream")), (a, b) -> true);
    final CountDownLatch started = new CountDownLatch(1);
    final CountDownLatch subscribed = new CountDownLatch(1);
    final AtomicBoolean cancelled = new AtomicBoolean();
    final AtomicBoolean shutDown = new AtomicBoolean();
    private final byte[] bytes;
    private final boolean holdBody;
    private final boolean holdHeaders;
    private final int status;
    volatile HttpRequest request;
    volatile CompletableFuture<?> pending;

    public FixtureClient(byte[] bytes, boolean holdBody, boolean holdHeaders, int status) {
        this.bytes = bytes;
        this.holdBody = holdBody;
        this.holdHeaders = holdHeaders;
        this.status = status;
    }

    public FixtureClient(byte[] bytes) {
        this(bytes, false, false, 200);
    }

    public boolean wasCancelled() { return cancelled.get(); }

    @Override
    public <T> CompletableFuture<HttpResponse<T>> sendAsync(HttpRequest value, HttpResponse.BodyHandler<T> handler) {
        request = value;
        if (holdHeaders) {
            var future = new CompletableFuture<HttpResponse<T>>();
            future.whenComplete((response, error) -> cancelled.set(future.isCancelled()));
            pending = future;
            started.countDown();
            return future;
        }
        var subscriber = handler.apply(new HttpResponse.ResponseInfo() {
            @Override public int statusCode() { return status; }
            @Override public HttpHeaders headers() { return HEADERS; }
            @Override public Version version() { return Version.HTTP_1_1; }
        });
        var future = subscriber.getBody().<HttpResponse<T>>thenApply(body ->
                new Response<>(value, body, status)).toCompletableFuture();
        pending = future;
        started.countDown();
        subscriber.onSubscribe(new Flow.Subscription() {
            private int offset;
            private long demand;
            private boolean draining;
            private boolean ended;

            @Override
            public synchronized void request(long count) {
                if (ended || cancelled.get()) return;
                demand = count > Long.MAX_VALUE - demand ? Long.MAX_VALUE : demand + count;
                if (draining) return;
                draining = true;
                try {
                    while (demand > 0 && offset < bytes.length && !cancelled.get()) {
                        demand--;
                        subscriber.onNext(List.of(ByteBuffer.wrap(bytes, offset++, 1)));
                    }
                    if (offset == bytes.length && !holdBody && !cancelled.get()) {
                        ended = true;
                        subscriber.onComplete();
                    }
                } finally {
                    draining = false;
                    subscribed.countDown();
                }
            }

            @Override
            public synchronized void cancel() {
                cancelled.set(true);
            }
        });
        return future;
    }

    public byte[] requestBytes() throws Exception {
        var output = new ByteArrayOutputStream();
        var done = new CompletableFuture<byte[]>();
        request.bodyPublisher().orElseThrow().subscribe(new Flow.Subscriber<ByteBuffer>() {
            @Override public void onSubscribe(Flow.Subscription subscription) { subscription.request(Long.MAX_VALUE); }
            @Override public void onNext(ByteBuffer item) {
                byte[] chunk = new byte[item.remaining()];
                item.get(chunk);
                output.writeBytes(chunk);
            }
            @Override public void onError(Throwable error) { done.completeExceptionally(error); }
            @Override public void onComplete() { done.complete(output.toByteArray()); }
        });
        return done.get(2, TimeUnit.SECONDS);
    }

    @Override
    public <T> HttpResponse<T> send(HttpRequest value, HttpResponse.BodyHandler<T> handler) throws IOException, InterruptedException {
        throw new UnsupportedOperationException("Only asynchronous requests are expected");
    }

    @Override
    public <T> CompletableFuture<HttpResponse<T>> sendAsync(
            HttpRequest value, HttpResponse.BodyHandler<T> handler, HttpResponse.PushPromiseHandler<T> push) {
        return sendAsync(value, handler);
    }

    @Override public Optional<CookieHandler> cookieHandler() { return Optional.empty(); }
    @Override public Optional<Duration> connectTimeout() { return Optional.empty(); }
    @Override public Redirect followRedirects() { return Redirect.NEVER; }
    @Override public Optional<ProxySelector> proxy() { return Optional.empty(); }
    @Override public Optional<Authenticator> authenticator() { return Optional.empty(); }
    @Override public Optional<Executor> executor() { return Optional.empty(); }
    @Override public Version version() { return Version.HTTP_1_1; }
    @Override public SSLParameters sslParameters() { return new SSLParameters(); }
    @Override public SSLContext sslContext() {
        try { return SSLContext.getDefault(); }
        catch (NoSuchAlgorithmException error) { throw new IllegalStateException(error); }
    }
    @Override public void shutdownNow() { shutDown.set(true); }

    private record Response<T>(HttpRequest request, T body, int statusCode) implements HttpResponse<T> {
        @Override public Optional<HttpResponse<T>> previousResponse() { return Optional.empty(); }
        @Override public HttpHeaders headers() { return HEADERS; }
        @Override public Optional<SSLSession> sslSession() { return Optional.empty(); }
        @Override public URI uri() { return request.uri(); }
        @Override public Version version() { return Version.HTTP_1_1; }
    }
}
