package dev.tinyagent;

import static dev.tinyagent.ProviderChecks.await;
import static dev.tinyagent.ProviderChecks.check;
import static dev.tinyagent.ProviderChecks.equal;
import static dev.tinyagent.ProviderChecks.expect;
import static dev.tinyagent.ProviderChecks.join;
import static dev.tinyagent.ProviderChecks.truth;
import static dev.tinyagent.ProviderChecks.worker;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import tools.jackson.databind.JsonNode;

/** Mandatory real socket checks for the public HTTP adapter. */
final class HttpChecks {
    private final HttpClient client;

    private HttpChecks(HttpClient client) { this.client = client; }

    static void run() throws Exception {
        try (var client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build()) {
            var checks = new HttpChecks(client);
            check("HTTP routes, loopback defaults, and bearer authentication", checks::routes);
            check("HTTP events and completed-history rollback", checks::history);
            check("HTTP tools and cancellation preserve prior history", checks::tools);
            check("HTTP native framing and bounded JSON bodies", checks::bodies);
            check("HTTP busy, cancellation, and independent sessions", checks::cancel);
            check("HTTP disconnect cancels silent provider work", checks::disconnect);
            check("HTTP oversized events roll back history", checks::oversized);
            check("HTTP cancellation releases stalled output", checks::stalled);
            check("HTTP shutdown joins deletion and partial request cleanup", checks::shutdown);
            check("HTTP provider callback can request shutdown", checks::callbackShutdown);
        }
    }

    private static HttpServer serve(Provider provider, String token) throws IOException {
        return HttpServer.start(new Agent(provider, "fixture", ""),
                new HttpServer.Options(new InetSocketAddress("127.0.0.1", 0), token, false));
    }

    private HttpResponse<String> request(HttpServer server, String method, String path, String body, String token)
            throws Exception {
        var builder = HttpRequest.newBuilder(server.url().resolve(path)).timeout(Duration.ofSeconds(5));
        if (token != null) builder.header("Authorization", "Bearer " + token);
        builder.method(method, body == null ? HttpRequest.BodyPublishers.noBody() : HttpRequest.BodyPublishers.ofString(body));
        if (body != null) builder.header("Content-Type", "application/json");
        return client.send(builder.build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private String session(HttpServer server, String token) throws Exception {
        var response = request(server, "POST", "/sessions", null, token);
        equal(response.statusCode(), 201);
        return Json.parse(response.body()).path("sessionId").stringValue();
    }

    private HttpResponse<String> prompt(HttpServer server, String id, String message) throws Exception {
        return request(server, "POST", "/sessions/" + id + "/prompts",
                Json.MAPPER.createObjectNode().put("message", message).toString(), null);
    }

    private static List<JsonNode> events(HttpResponse<String> response) throws Exception {
        equal(response.statusCode(), 200);
        equal(response.headers().firstValue("content-type").orElseThrow(), "application/jsonl; charset=utf-8");
        var result = new ArrayList<JsonNode>();
        for (String line : response.body().lines().toList()) result.add(Json.parse(line));
        equal(result.getFirst().path("type").stringValue(), "agent_start");
        equal(result.getLast().path("type").stringValue(), "agent_end");
        for (int index = 0; index < result.size(); index++) {
            equal(result.get(index).path("sequence").intValue(), index + 1);
            equal(result.get(index).path("runId"), result.getFirst().path("runId"));
        }
        return result;
    }

    private static Socket socket(HttpServer server, String bytes) throws IOException {
        var socket = new Socket();
        try {
            socket.connect(new InetSocketAddress(server.url().getHost(), server.url().getPort()), 3000);
            socket.setSoTimeout(5000);
            socket.getOutputStream().write(bytes.getBytes(StandardCharsets.ISO_8859_1));
            return socket;
        } catch (IOException error) { socket.close(); throw error; }
    }

    private static String firstLine(Socket socket) throws IOException {
        return new BufferedReader(new InputStreamReader(socket.getInputStream(), StandardCharsets.ISO_8859_1)).readLine();
    }

    private static String wire(String id, String message) {
        var body = "{\"message\":\"" + message + "\"}";
        return "POST /sessions/" + id + "/prompts HTTP/1.1\r\nHost: local\r\nContent-Type: application/json\r\nContent-Length: "
                + body.length() + "\r\n\r\n" + body;
    }

    private void routes() throws Exception {
        try (var server = serve((request, token, text) -> CoreChecks.turn("done"), "secret")) {
            equal(server.url().getHost(), "127.0.0.1");
            equal(request(server, "GET", "/health", null, null).statusCode(), 401);
            var health = request(server, "GET", "/health", null, "secret");
            equal(health.statusCode(), 200);
            equal(Json.parse(health.body()).path("protocolVersion").intValue(), 1);
            truth(health.headers().firstValue("access-control-allow-origin").isEmpty(), "CORS header was enabled");
            var id = session(server, "secret");
            equal(request(server, "DELETE", "/sessions/" + id, null, "secret").statusCode(), 204);
            equal(request(server, "POST", "/sessions/" + id + "/cancel", null, "secret").statusCode(), 404);
            equal(request(server, "GET", "/missing", null, "secret").statusCode(), 404);
            try (var duplicate = socket(server, "GET /health HTTP/1.1\r\nHost: local\r\nAuthorization: Bearer secret\r\nAuthorization: Bearer secret\r\n\r\n")) {
                truth(firstLine(duplicate).contains(" 401 "), "Duplicate bearer headers were accepted");
            }
        }
        expect(IllegalArgumentException.class, () -> new HttpServer.Options(new InetSocketAddress("0.0.0.0", 0), null, false));
        expect(IllegalArgumentException.class, () -> new HttpServer.Options(new InetSocketAddress("0.0.0.0", 0), "secret", false));
        expect(IllegalArgumentException.class, () -> new HttpServer.Options(new InetSocketAddress("127.0.0.1", 0), "bad\r\nheader", false));
    }

    private void history() throws Exception {
        var sizes = new ArrayList<Integer>();
        try (var server = serve((request, token, text) -> {
            sizes.add(request.history().size());
            text.accept("café\n");
            if (((Message.User) request.history().getLast()).text().equals("fail")) throw new ProviderException("fixture failure");
            return CoreChecks.turn("final");
        }, null)) {
            var id = session(server, null);
            equal(events(prompt(server, id, "first")).getLast().path("status").stringValue(), "completed");
            equal(events(prompt(server, id, "fail")).getLast().path("status").stringValue(), "failed");
            equal(events(prompt(server, id, "last")).getLast().path("text").stringValue(), "final");
            equal(sizes, List.of(1, 3, 3));
        }
    }

    private void tools() throws Exception {
        var entered = new CountDownLatch(1);
        var cleaned = new AtomicBoolean();
        var sizes = new ArrayList<Integer>();
        var echo = new Tool("echo", "Return text", Json.parse("""
                {"type":"object","properties":{"text":{"type":"string","minLength":1}},"required":["text"]}
                """), (arguments, token) -> {
            if (arguments.path("text").stringValue().equals("wait")) {
                var wake = new CountDownLatch(1);
                var registration = token.onCancel(wake::countDown);
                entered.countDown();
                try { wake.await(); token.check(); }
                finally { registration.close(); cleaned.set(true); }
            }
            return arguments.path("text").stringValue();
        });
        Provider provider = (request, token, text) -> {
            if (request.history().getLast() instanceof Message.ToolResult result) return CoreChecks.turn(result.output());
            sizes.add(request.history().size());
            String message = ((Message.User) request.history().getLast()).text();
            return new ProviderTurn("", List.of(new ToolCall("call", "echo",
                    Json.MAPPER.createObjectNode().put("text", message.equals("invalid") ? "" : message))), List.of(), List.of());
        };
        try (var server = HttpServer.start(new Agent(provider, "fixture", "", List.of(echo)),
                new HttpServer.Options(new InetSocketAddress("127.0.0.1", 0), null, false))) {
            var id = session(server, null);
            for (String message : List.of("café", "invalid")) {
                var values = events(prompt(server, id, message));
                equal(values.get(1).path("type").stringValue(), "tool_execution_start");
                equal(values.get(2).path("type").stringValue(), "tool_execution_end");
                equal(values.get(2).path("toolCallId"), values.get(1).path("toolCallId"));
                equal(values.get(2).path("isError").booleanValue(), message.equals("invalid"));
                equal(values.getLast().path("text"), values.get(2).path("output"));
                equal(values.getLast().path("status").stringValue(), "completed");
            }
            var failure = new AtomicReference<Throwable>();
            var response = new AtomicReference<HttpResponse<String>>();
            var pending = worker(() -> response.set(prompt(server, id, "wait")), failure);
            try {
                await(entered);
                equal(request(server, "POST", "/sessions/" + id + "/cancel", null, null).statusCode(), 200);
                join(pending);
                truth(failure.get() == null && cleaned.get(), "Cancellation failed to join tool cleanup");
                var values = events(response.get());
                equal(values.size(), 4);
                equal(values.get(2).path("type").stringValue(), "tool_execution_end");
                truth(values.get(2).path("isError").booleanValue(), "Cancelled tool reported success");
                equal(values.getLast().path("status").stringValue(), "aborted");
                equal(events(prompt(server, id, "next")).getLast().path("status").stringValue(), "completed");
                equal(sizes, List.of(1, 5, 9, 9));
            } finally { server.stop(); join(pending); }
        }
    }

    private void bodies() throws Exception {
        try (var server = serve((request, token, text) -> CoreChecks.turn("done"), null)) {
            var id = session(server, null);
            var path = "/sessions/" + id + "/prompts";
            for (String body : List.of("{", "[]", "{\"message\":\"\"}", "{\"message\":\"\\ud800\"}")) {
                equal(request(server, "POST", path, body, null).statusCode(), 400);
            }
            var header = "POST " + path + " HTTP/1.1\r\nHost: local\r\nContent-Type: application/json\r\n";
            try (var large = socket(server, header + "Content-Length: 1048577\r\n\r\n")) {
                truth(firstLine(large).contains(" 413 "), "Oversized declared body was not rejected early");
            }
            try (var invalid = socket(server, header + "Content-Length: 15\r\n\r\n{\"message\":\"\u00ff\"}")) {
                truth(firstLine(invalid).contains(" 400 "), "Malformed UTF-8 was accepted");
            }
            try (var invalid = socket(server, header + "Transfer-Encoding: chunked\r\n\r\nnope\r\n")) {
                truth(firstLine(invalid).contains(" 400 "), "Malformed chunk framing was accepted");
            }
            for (String media : List.of("Content-Type: text/plain\r\n",
                    "Content-Type: application/json\r\nContent-Type: text/plain\r\n")) {
                try (var invalid = socket(server, "POST " + path + " HTTP/1.1\r\nHost: local\r\n" + media + "Content-Length: 2\r\n\r\n{}")) {
                    truth(firstLine(invalid).contains(" 415 "), "Invalid media type was accepted");
                }
            }
            var chunks = new StringBuilder();
            for (byte value : "{\"message\":\"café\"}".getBytes(StandardCharsets.UTF_8)) {
                chunks.append("1\r\n").append((char) Byte.toUnsignedInt(value)).append("\r\n");
            }
            try (var chunked = socket(server, header + "Transfer-Encoding: chunked\r\n\r\n" + chunks + "0\r\n\r\n")) {
                var response = new String(chunked.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
                truth(response.startsWith("HTTP/1.1 200 ") && response.contains("\"status\":\"completed\""),
                        "One-byte UTF-8 chunks did not complete");
            }
            var data = Json.MAPPER.createObjectNode().put("message", "go").put("padding", "");
            data.put("padding", "x".repeat(HttpJson.MAX_BODY_BYTES - data.toString().length()));
            equal(events(request(server, "POST", path, data.toString(), null)).getLast().path("status").stringValue(), "completed");
            var overflow = data + " ";
            try (var large = socket(server, header + "Transfer-Encoding: chunked\r\n\r\n" + Integer.toHexString(overflow.length()) + "\r\n" + overflow + "\r\n0\r\n\r\n")) {
                truth(firstLine(large).contains(" 413 "), "Oversized chunked body was accepted");
            }
        }
    }

    private void cancel() throws Exception {
        var entered = new CountDownLatch(1);
        try (var server = serve((request, token, text) -> {
            if (((Message.User) request.history().getLast()).text().equals("wait")) {
                entered.countDown();
                while (!token.isCancelled()) Thread.sleep(5);
                token.check();
            }
            return CoreChecks.turn("done");
        }, null)) {
            var first = session(server, null);
            var second = session(server, null);
            var failure = new AtomicReference<Throwable>();
            var result = new AtomicReference<HttpResponse<String>>();
            var pending = worker(() -> result.set(prompt(server, first, "wait")), failure);
            try {
                await(entered);
                equal(prompt(server, first, "busy").statusCode(), 409);
                equal(events(prompt(server, second, "go")).getLast().path("status").stringValue(), "completed");
                var cancelled = Json.parse(request(server, "POST", "/sessions/" + first + "/cancel", null, null).body());
                truth(cancelled.path("accepted").booleanValue(), "Cancellation missed the active run");
                join(pending);
                truth(failure.get() == null, "Prompt request failed: " + failure.get());
                var terminal = events(result.get()).getLast();
                equal(terminal.path("status").stringValue(), "aborted");
                equal(terminal.path("runId"), cancelled.path("runId"));
            } finally { server.stop(); join(pending); }
        }
    }

    private void disconnect() throws Exception {
        var entered = new CountDownLatch(1);
        var cancelled = new CountDownLatch(1);
        try (var server = serve((request, token, text) -> {
            entered.countDown();
            try { while (!token.isCancelled()) Thread.sleep(5); token.check(); }
            finally { cancelled.countDown(); }
            return CoreChecks.turn("unreachable");
        }, null)) {
            var id = session(server, null);
            try (var socket = socket(server, wire(id, "wait"))) {
                truth(firstLine(socket).contains(" 200 "), "Prompt headers were not delivered");
                await(entered);
            }
            await(cancelled);
            equal(request(server, "DELETE", "/sessions/" + id, null, null).statusCode(), 204);
        }
    }

    private void oversized() throws Exception {
        var sizes = new ArrayList<Integer>();
        try (var server = serve((request, token, text) -> {
            sizes.add(request.history().size());
            if (((Message.User) request.history().getLast()).text().equals("large")) text.accept("x".repeat(HttpJson.MAX_EVENT_BYTES));
            return CoreChecks.turn("done");
        }, null)) {
            var id = session(server, null);
            events(prompt(server, id, "first"));
            equal(events(prompt(server, id, "large")).getLast().path("error").path("code").stringValue(), "output_too_large");
            events(prompt(server, id, "last"));
            equal(sizes, List.of(1, 3, 3));
        }
    }

    private void stalled() throws Exception {
        var entered = new CountDownLatch(1);
        var cleaned = new CountDownLatch(1);
        try (var server = serve((request, token, text) -> {
            if (((Message.User) request.history().getLast()).text().equals("flood")) {
                entered.countDown();
                try { while (true) text.accept("x".repeat(524_288)); }
                finally { cleaned.countDown(); }
            }
            equal(request.history().size(), 1);
            return CoreChecks.turn("done");
        }, null)) {
            var id = session(server, null);
            try (var socket = socket(server, wire(id, "flood"))) {
                socket.setReceiveBufferSize(4096);
                await(entered);
                Thread.sleep(100);
                truth(Json.parse(request(server, "POST", "/sessions/" + id + "/cancel", null, null).body())
                        .path("accepted").booleanValue(), "Stalled run was not cancelled");
                await(cleaned);
                HttpResponse<String> response;
                long deadline = System.nanoTime() + Duration.ofSeconds(5).toNanos();
                do {
                    response = prompt(server, id, "fresh");
                    truth(System.nanoTime() < deadline, "Cancelled session stayed busy");
                    if (response.statusCode() == 409) Thread.sleep(5);
                } while (response.statusCode() == 409);
                equal(events(response).getLast().path("status").stringValue(), "completed");
            }
        }
    }

    private void shutdown() throws Exception {
        var entered = new CountDownLatch(1);
        var cancelled = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        var cleaned = new AtomicBoolean();
        var server = serve((request, token, text) -> {
            entered.countDown();
            while (!token.isCancelled()) Thread.sleep(5);
            cancelled.countDown();
            await(release);
            cleaned.set(true);
            token.check();
            return CoreChecks.turn("unreachable");
        }, null);
        var promptFailure = new AtomicReference<Throwable>();
        var deleteFailure = new AtomicReference<Throwable>();
        var closeFailure = new AtomicReference<Throwable>();
        Thread pending = null;
        Thread deleting = null;
        Thread closing = null;
        try {
            var id = session(server, null);
            pending = worker(() -> prompt(server, id, "wait"), promptFailure);
            await(entered);
            deleting = worker(() -> {
                try { request(server, "DELETE", "/sessions/" + id, null, null); }
                catch (IOException ignored) { /* Shutdown may close this response before cleanup returns. */ }
            }, deleteFailure);
            await(cancelled);
            try (var partial = socket(server, "POST /sessions HTTP/1.1\r\nHost: local\r\nContent-Length: 10\r\n")) {
                closing = worker(server::close, closeFailure);
                Thread.sleep(50);
                truth(closing.isAlive() && !cleaned.get(), "Shutdown abandoned provider cleanup");
                release.countDown();
                join(closing);
                truth(closeFailure.get() == null, "Shutdown failed: " + closeFailure.get());
                partial.getInputStream().readAllBytes();
            }
        } finally {
            release.countDown();
            server.close();
            if (pending != null) join(pending);
            if (deleting != null) join(deleting);
            if (closing != null) join(closing);
        }
        truth(cleaned.get(), "Provider cleanup did not finish");
        truth(deleteFailure.get() == null, "Deletion failed: " + deleteFailure.get());
    }

    private void callbackShutdown() throws Exception {
        var owner = new AtomicReference<HttpServer>();
        try (var server = serve((request, token, text) -> {
            owner.get().stop();
            return CoreChecks.turn("done");
        }, null)) {
            owner.set(server);
            equal(events(prompt(server, session(server, null), "stop")).getLast().path("status").stringValue(), "aborted");
            server.awaitStopped();
        }
    }
}
