package dev.tinyagent;

import java.io.Closeable;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Objects;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import org.eclipse.jetty.http.HttpHeader;
import org.eclipse.jetty.server.Handler;
import org.eclipse.jetty.server.HttpConfiguration;
import org.eclipse.jetty.server.HttpConnectionFactory;
import org.eclipse.jetty.server.Request;
import org.eclipse.jetty.server.Response;
import org.eclipse.jetty.server.Server;
import org.eclipse.jetty.server.ServerConnector;
import org.eclipse.jetty.util.Callback;

/** In-memory HTTP sessions around a configured Agent. The caller owns its provider. */
public final class HttpServer implements Closeable {
    public record Options(InetSocketAddress address, String token, boolean allowRemote) {
        public Options() { this(new InetSocketAddress("127.0.0.1", 8080), null, false); }

        public Options {
            Objects.requireNonNull(address);
            if (address.isUnresolved()) throw new IllegalArgumentException("HTTP address must be resolved");
            if (token != null && (token.isEmpty() || !token.chars().allMatch(value -> value >= 33 && value <= 126))) {
                throw new IllegalArgumentException("Token must contain printable non-space ASCII");
            }
            if (!address.getAddress().isLoopbackAddress() && !(allowRemote && token != null)) {
                throw new IllegalArgumentException("Remote binding requires allowRemote and a token");
            }
        }
    }

    private final Agent agent;
    private final byte[] bearer;
    private final Server server = new Server();
    private final ServerConnector connector;
    private final URI url;
    private final Object lock = new Object();
    private final HashMap<String, Session> sessions = new HashMap<>();
    private final HashSet<HttpConnection> connections = new HashSet<>();
    private final CompletableFuture<Void> stopped = new CompletableFuture<>();
    private boolean stopping;

    public static HttpServer start(Agent agent) throws IOException { return start(agent, new Options()); }

    public static HttpServer start(Agent agent, Options options) throws IOException {
        return new HttpServer(agent, options);
    }

    private HttpServer(Agent agent, Options options) throws IOException {
        this.agent = Objects.requireNonNull(agent);
        Objects.requireNonNull(options);
        bearer = options.token() == null ? null : ("Bearer " + options.token()).getBytes(StandardCharsets.US_ASCII);
        var configuration = new HttpConfiguration();
        configuration.setPersistentConnectionsEnabled(false);
        configuration.setSendServerVersion(false);
        connector = new ServerConnector(server, new HttpConnectionFactory(configuration));
        connector.setHost(options.address().getAddress().getHostAddress());
        connector.setPort(options.address().getPort());
        connector.setIdleTimeout(0);
        server.addConnector(connector);
        server.setHandler(new Handler.Abstract() {
            @Override public boolean handle(Request request, Response response, Callback callback) {
                handleRequest(request, response, callback);
                return true;
            }
        });
        server.setErrorHandler((request, response, callback) -> {
            int status = response.getStatus() >= 500 ? 500 : 400;
            HttpJson.error(response, status, status == 500 ? "server_error" : "invalid_request",
                    status == 500 ? "HTTP request failed" : "Invalid HTTP request", callback);
            return true;
        });
        try {
            server.start();
            url = new URI("http", null, options.address().getAddress().getHostAddress(),
                    connector.getLocalPort(), "/", null, null);
        } catch (Exception failure) {
            try { server.stop(); }
            catch (Exception cleanup) { failure.addSuppressed(cleanup); }
            throw new IOException("Could not start HTTP server", failure);
        }
    }

    public URI url() { return url; }

    /** Request shutdown from a callback; an external owner awaits or closes. */
    public void stop() {
        ArrayList<Session> owned;
        ArrayList<HttpConnection> pending;
        synchronized (lock) {
            if (stopping) return;
            stopping = true;
            owned = new ArrayList<>(sessions.values());
            pending = new ArrayList<>(connections);
        }
        connector.close();
        RuntimeException failure = null;
        for (var session : owned) {
            try { session.stop(); }
            catch (RuntimeException error) {
                if (failure == null) failure = error;
                else if (error != failure) failure.addSuppressed(error);
            }
        }
        for (var connection : pending) {
            try { connection.stop(); }
            catch (RuntimeException error) {
                if (failure == null) failure = error;
                else if (error != failure) failure.addSuppressed(error);
            }
        }
        RuntimeException cancellationFailure = failure;
        Thread.ofVirtual().start(() -> {
            Throwable outcome = cancellationFailure;
            try {
                for (var session : owned) session.awaitIdle();
                server.stop();
            } catch (Exception error) {
                if (outcome == null) outcome = error;
                else outcome.addSuppressed(error);
            } finally {
                synchronized (lock) { sessions.clear(); }
                if (outcome == null) stopped.complete(null);
                else stopped.completeExceptionally(outcome);
            }
        });
    }

    public void awaitStopped() throws IOException {
        try { stopped.join(); }
        catch (CompletionException failure) { throw new IOException("HTTP shutdown failed", failure.getCause()); }
    }

    @Override public void close() throws IOException { stop(); awaitStopped(); }

    private void handleRequest(Request request, Response response, Callback callback) {
        var connection = new HttpConnection(request, response);
        synchronized (lock) {
            if (stopping) {
                callback.failed(new IOException("HTTP server is stopping"));
                return;
            }
            connections.add(connection);
        }
        try {
            authenticate(request);
            route(request, response, callback, connection);
        } catch (Session.BusyException failure) {
            HttpJson.error(response, 409, "session_busy", "The session already has an active run", callback);
        } catch (Session.ClosedException failure) {
            HttpJson.error(response, 404, "session_not_found", "Unknown session", callback);
        } catch (HttpJson.Failure failure) {
            HttpJson.error(response, failure.status, failure.code, failure.getMessage(), callback);
        } catch (Exception failure) {
            callback.failed(failure);
        } finally {
            try { connection.finish(); }
            finally { synchronized (lock) { connections.remove(connection); } }
        }
    }

    private void authenticate(Request request) throws HttpJson.Failure {
        if (bearer == null) return;
        var values = request.getHeaders().getValuesList(HttpHeader.AUTHORIZATION);
        if (values.size() != 1 || !MessageDigest.isEqual(bearer, values.getFirst().getBytes(StandardCharsets.UTF_8))) {
            throw new HttpJson.Failure(401, "unauthorized", "Bearer authentication required");
        }
    }

    private void route(Request request, Response response, Callback callback, HttpConnection connection) throws Exception {
        String method = request.getMethod();
        String path = request.getHttpURI().getPath();
        if (method.equals("GET") && path.equals("/health")) {
            HttpJson.write(response, 200, Json.MAPPER.createObjectNode().put("status", "ok").put("protocolVersion", 1), callback);
            return;
        }
        if (method.equals("POST") && path.equals("/sessions")) {
            HttpJson.read(request);
            String id = "s_" + UUID.randomUUID();
            synchronized (lock) {
                if (stopping) throw new IOException("HTTP server is stopping");
                sessions.put(id, new Session(agent));
            }
            HttpJson.write(response, 201, Json.MAPPER.createObjectNode().put("sessionId", id), callback);
            return;
        }
        String[] parts = path.split("/", -1);
        if (parts.length < 3 || parts.length > 4 || !parts[0].isEmpty() || !parts[1].equals("sessions")) {
            throw new HttpJson.Failure(404, "not_found", "Unknown route");
        }
        String id = parts[2];
        Session session;
        synchronized (lock) { session = sessions.get(id); }
        if (session == null) throw new HttpJson.Failure(404, "session_not_found", "Unknown session");
        if (method.equals("DELETE") && parts.length == 3) {
            session.close();
            synchronized (lock) { sessions.remove(id); }
            HttpJson.write(response, 204, null, callback);
        } else if (method.equals("POST") && parts.length == 4 && parts[3].equals("cancel")) {
            HttpJson.read(request);
            String runId = session.cancel();
            HttpJson.write(response, 200, Json.MAPPER.createObjectNode()
                    .put("accepted", runId != null).put("runId", runId), callback);
        } else if (method.equals("POST") && parts.length == 4 && parts[3].equals("prompts")) {
            var message = HttpJson.read(request).path("message");
            if (!message.isString() || message.stringValue().isEmpty()) {
                throw new HttpJson.Failure(400, "invalid_request", "message must be a nonempty string");
            }
            connection.run(session, message.stringValue());
            callback.succeeded();
        } else {
            throw new HttpJson.Failure(404, "not_found", "Unknown route");
        }
    }
}
