package dev.tinyagent;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CancellationException;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import tools.jackson.databind.node.ObjectNode;

/** Native SDK checks; --offline excludes provider and HTTP server sockets. */
public final class ProviderChecks {
    static final URI ENDPOINT = URI.create("http://127.0.0.1:8080/fixture");
    static final Duration TIMEOUT = Duration.ofSeconds(3);
    private static final Path FIXTURES = Path.of("../../conformance/providers");
    private static int passed;
    private static int failed;

    private ProviderChecks() {}

    @FunctionalInterface
    interface Check { void run() throws Exception; }

    public static void main(String[] args) throws Exception {
        if (args.length == 1 && (args[0].equals("--bash-only") || args[0].equals("--http-only") || args[0].equals("--tools-only"))) {
            if (args[0].equals("--bash-only")) BashChecks.run();
            else if (args[0].equals("--tools-only")) ToolChecks.run();
            else HttpChecks.run();
            System.out.println(passed + " passed, " + failed + " failed");
            if (failed != 0) throw new AssertionError("Focused checks failed");
            return;
        }
        if (args.length > 1 || (args.length == 1 && !args[0].equals("--offline"))) {
            throw new IllegalArgumentException("Usage: ProviderChecks [--offline | --bash-only | --http-only | --tools-only]");
        }
        check("OpenAI text and tool fixtures", () -> fixtureRoundTrip(false));
        check("Anthropic text and tool fixtures", () -> fixtureRoundTrip(true));
        check("SSE encoding, framing, and bounds", ProviderChecks::sseFraming);
        check("Malformed and incomplete provider responses", ProviderChecks::malformed);
        check("Immutable request and result snapshots", ProviderChecks::snapshots);
        check("Cancellation registration cleanup", ProviderChecks::registrations);
        check("Cancellation before headers and during body", ProviderChecks::cancellation);
        check("Request timeout includes body reads", ProviderChecks::timeouts);
        check("Consumer failure closes body", ProviderChecks::consumerFailure);
        check("Interrupted request releases resources", ProviderChecks::interrupt);
        check("Terminal event does not wait for EOF", ProviderChecks::terminalWithoutEof);
        check("HTTP errors and client ownership", ProviderChecks::httpErrorsAndOwnership);
        CoreChecks.run();
        SessionChecks.run();
        RpcChecks.run();
        BashChecks.run();
        ToolChecks.run();
        if (args.length == 0) {
            check("OpenAI real TCP streaming and cancellation", () -> ProviderTcpChecks.run(false));
            check("Anthropic real TCP streaming and cancellation", () -> ProviderTcpChecks.run(true));
            HttpChecks.run();
        }
        System.out.println(passed + " passed, " + failed + " failed");
        if (failed != 0) throw new AssertionError("SDK checks failed");
    }

    static void check(String name, Check test) {
        try {
            test.run();
            passed++;
            System.out.println("PASS " + name);
        } catch (Exception | AssertionError error) {
            failed++;
            System.err.println("FAIL " + name);
            error.printStackTrace(System.err);
        }
    }

    static byte[] fixture(boolean anthropic, boolean tool) throws IOException {
        return Files.readAllBytes(FIXTURES.resolve((anthropic ? "anthropic" : "openai") + (tool ? "_tool.sse" : "_text.sse")));
    }

    static ProviderRequest request(List<Message> history) throws ProviderException {
        var schema = Json.parse("""
                {"type":"object","properties":{"text":{"type":"string"}},"required":["text"],"additionalProperties":false}
                """);
        return new ProviderRequest("fixture-model", "Fixture instructions", history,
                List.of(new ToolDefinition("echo", "Return text", schema)));
    }

    static HttpProvider provider(boolean anthropic, FixtureClient client, Duration timeout) {
        return anthropic
                ? new AnthropicProvider(client, ENDPOINT, "fixture-key", timeout, 1024)
                : new OpenAIProvider(client, ENDPOINT, "fixture-key", timeout);
    }

    private static ProviderTurn read(boolean anthropic, String wire) throws Exception {
        var client = new FixtureClient(wire.getBytes(StandardCharsets.UTF_8));
        return provider(anthropic, client, TIMEOUT).stream(request(List.of()), new CancellationToken(), delta -> {});
    }

    private static void fixtureRoundTrip(boolean anthropic) throws Exception {
        var client = new FixtureClient(fixture(anthropic, true));
        var provider = provider(anthropic, client, TIMEOUT);
        var deltas = new StringBuilder();
        var turn = provider.stream(request(List.of(new Message.User("echo café"))), new CancellationToken(), deltas::append);
        equal(turn.toolCalls().size(), 1);
        equal(turn.toolCalls().getFirst().callId(), "call_1");
        equal(turn.toolCalls().getFirst().arguments(), Json.parse("{\"text\":\"café\"}"));
        equal(deltas.toString(), anthropic ? "" : "Working ");
        var body = Json.MAPPER.readTree(client.requestBytes());
        equal(body.path("model").stringValue(), "fixture-model");
        equal(body.path("stream").booleanValue(), true);
        equal(body.path(anthropic ? "system" : "instructions").stringValue(), "Fixture instructions");
        equal(body.path("tools").get(0).path(anthropic ? "input_schema" : "parameters"), request(List.of()).tools().getFirst().parameters());
        equal(client.request.headers().firstValue(anthropic ? "x-api-key" : "Authorization").orElseThrow(),
                anthropic ? "fixture-key" : "Bearer fixture-key");
        if (anthropic) {
            equal(client.request.headers().firstValue("anthropic-version").orElseThrow(), "2023-06-01");
            equal(body.path("max_tokens").intValue(), 1024);
            equal(turn.replay().getFirst().path("fixture_metadata").stringValue(), "keep");
            equal(turn.replay().getFirst().path("usage").path("input_tokens").intValue(), 8);
            equal(turn.replay().getFirst().path("usage").path("output_tokens").intValue(), 12);
            equal(turn.output().getFirst().path("signature").stringValue(), "signed-fixture");
        } else {
            equal(body.path("store").booleanValue(), false);
            equal(body.path("include"), Json.parse("[\"reasoning.encrypted_content\"]"));
            equal(body.path("parallel_tool_calls").booleanValue(), false);
            equal(turn.replay().getFirst().path("encrypted_content").stringValue(), "opaque-fixture");
        }
        var history = List.<Message>of(new Message.User("echo café"),
                new Message.Assistant(turn.text(), turn.toolCalls(), turn.replay()),
                new Message.ToolResult("call_1", "echo", "café", false),
                new Message.ToolResult("call_2", "echo", "second", true));
        var next = provider.requestBody(request(history));
        if (anthropic) {
            equal(next.path("messages").get(1).path("content"), turn.replay().getFirst().path("content"));
            equal(next.path("messages").size(), 3);
            equal(next.path("messages").get(2).path("content").size(), 2);
            equal(next.path("messages").get(2).path("content").get(1).path("is_error").booleanValue(), true);
        } else {
            equal(next.path("input").get(1), turn.replay().getFirst());
            equal(next.path("include"), Json.parse("[\"reasoning.encrypted_content\"]"));
            equal(next.path("input").get(2), turn.replay().get(1));
            equal(next.path("input").get(3).path("output").stringValue(), "café");
        }
        var textClient = new FixtureClient(fixture(anthropic, false));
        var text = new StringBuilder();
        var finalTurn = provider(anthropic, textClient, TIMEOUT).stream(request(history), new CancellationToken(), text::append);
        equal(finalTurn.text(), "done\ncafé");
        equal(text.toString(), finalTurn.text());
        equal(finalTurn.toolCalls().size(), 0);
    }

    private static void sseFraming() throws Exception {
        String wire = new String(fixture(false, false), StandardCharsets.UTF_8);
        equal(read(false, wire.replace("\n", "\r\n")).text(), "done\ncafé");
        equal(read(false, wire.stripTrailing()).text(), "done\ncafé");
        expect(ProviderException.class, () -> read(false, "data: {}\n\n"));
        expect(ProviderException.class, () -> read(false, "data: {} {}\n\n"));
        expect(ProviderException.class, () -> read(false, "data: [DONE]\n\n"));
        expect(ProviderException.class, () -> read(false, "data: {"));
        expect(ProviderException.class, () -> read(false, ":" + "x".repeat(Sse.MAX_BYTES) + "\n"));
        String fragment = "x".repeat(Sse.MAX_BYTES / 2);
        expect(ProviderException.class, () -> read(false, "data: " + fragment + "\ndata: " + fragment + "\n\n"));
        byte[] invalid = {':', (byte) 0xc3, 0x28, '\n'};
        expect(ProviderException.class, () -> Sse.read(new ByteArrayInputStream(invalid), (event, sink) -> null,
                new CancellationToken(), delta -> {}));
        var arguments = new Arguments();
        arguments.append("é".repeat(Sse.MAX_BYTES / 2));
        expect(ProviderException.class, () -> arguments.append("a"));
    }

    private static void malformed() throws Exception {
        for (String wire : List.of(
                "data: {\"type\":\"response.failed\"}\n\n",
                "data: {\"type\":\"response.incomplete\"}\n\n",
                "data: {\"type\":\"response.completed\",\"response\":{\"status\":\"incomplete\",\"output\":[]}}\n\n",
                "data: {\"type\":\"response.output_text.delta\",\"delta\":1}\n\n",
                "data: {\"type\":\"response.output_item.added\",\"output_index\":-1,\"item\":{}}\n\n",
                "data: {\"type\":\"response.completed\",\"response\":{\"output\":[{\"type\":\"function_call\",\"call_id\":\"x\",\"name\":\"echo\",\"arguments\":\"[]\"}]}}\n\n",
                "data: {\"type\":\"response.completed\",\"response\":{\"output\":[{\"type\":\"function_call\",\"call_id\":\"x\",\"name\":\"echo\",\"arguments\":\"{\"}]}}\n\n")) {
            expect(ProviderException.class, () -> read(false, wire));
        }
        String function = "{\"type\":\"function_call\",\"call_id\":\"x\",\"name\":\"echo\",\"arguments\":\"{}\"}";
        expect(ProviderException.class, () -> read(false, "data: {\"type\":\"response.completed\",\"response\":{\"output\":["
                + function + "," + function + "]}}\n\n"));
        String tool = new String(fixture(true, true), StandardCharsets.UTF_8);
        for (String wire : List.of(
                "data: {\"type\":\"message_stop\"}\n\n",
                tool.replace("\"stop_reason\":\"tool_use\"", "\"stop_reason\":\"max_tokens\""),
                tool.replace("\"index\":1", "\"index\":-1"),
                tool.replace("\"type\":\"input_json_delta\"", "\"type\":\"text_delta\""),
                tool.replace("data: {\"type\":\"content_block_stop\",\"index\":1}\n\n", ""),
                tool.replace("data: {\"type\":\"message_stop\"}", ""))) {
            expect(ProviderException.class, () -> read(true, wire));
        }
    }

    private static void snapshots() throws Exception {
        var node = (ObjectNode) Json.parse("{\"text\":\"before\"}");
        var call = new ToolCall("call", "echo", node);
        var tool = new ToolDefinition("echo", "", node);
        var assistant = new Message.Assistant("", List.of(call), List.of(node));
        var turn = new ProviderTurn("", List.of(call), List.of(node), List.of(node));
        var history = new ArrayList<Message>(List.of(assistant));
        var request = request(history);
        history.clear();
        node.put("text", "after");
        equal(call.arguments().path("text").stringValue(), "before");
        equal(tool.parameters().path("text").stringValue(), "before");
        equal(assistant.replay().getFirst().path("text").stringValue(), "before");
        equal(request.history().size(), 1);
        ((ObjectNode) call.arguments()).put("text", "mutated accessor");
        ((ObjectNode) tool.parameters()).put("text", "mutated accessor");
        ((ObjectNode) assistant.replay().getFirst()).put("text", "mutated accessor");
        ((ObjectNode) turn.output().getFirst()).put("text", "mutated accessor");
        ((ObjectNode) turn.replay().getFirst()).put("text", "mutated accessor");
        equal(call.arguments().path("text").stringValue(), "before");
        equal(tool.parameters().path("text").stringValue(), "before");
        equal(assistant.replay().getFirst().path("text").stringValue(), "before");
        equal(turn.output().getFirst().path("text").stringValue(), "before");
        equal(turn.replay().getFirst().path("text").stringValue(), "before");
        expect(UnsupportedOperationException.class, () -> request.history().clear());
    }

    private static void registrations() throws Exception {
        var token = new CancellationToken();
        var called = new ArrayList<Integer>();
        var removed = token.onCancel(() -> called.add(0));
        removed.close();
        var failure = new IllegalStateException("cleanup");
        token.onCancel(() -> { called.add(1); throw failure; });
        token.onCancel(() -> { throw failure; });
        token.onCancel(() -> called.add(2));
        equal(expect(IllegalStateException.class, token::cancel), failure);
        token.cancel();
        token.onCancel(() -> called.add(3));
        equal(called, List.of(1, 2, 3));
        expect(CancellationException.class, token::check);
    }

    private static void cancellation() throws Exception {
        for (boolean anthropic : List.of(false, true)) {
            for (boolean headers : List.of(false, true)) {
                var client = new FixtureClient(new byte[0], true, headers, 200);
                var token = new CancellationToken();
                var failure = new AtomicReference<Throwable>();
                var worker = worker(() -> provider(anthropic, client, TIMEOUT).stream(request(List.of()), token, delta -> {}), failure);
                await(headers ? client.started : client.subscribed);
                token.cancel();
                join(worker);
                truth(failure.get() instanceof CancellationException, "Cancellation was not propagated: " + failure.get());
                truth(client.cancelled.get(), "HTTP resource was not cancelled");
            }
            var client = new FixtureClient(new byte[0]);
            var token = new CancellationToken();
            token.cancel();
            expect(CancellationException.class, () -> provider(anthropic, client, TIMEOUT).stream(request(List.of()), token, delta -> {}));
            truth(client.request == null, "A pre-cancelled run sent a request");
        }
    }

    private static void timeouts() throws Exception {
        for (boolean headers : List.of(false, true)) {
            var client = new FixtureClient(new byte[0], true, headers, 200);
            var error = expect(ProviderException.class, () -> provider(false, client, Duration.ofMillis(100))
                    .stream(request(List.of()), new CancellationToken(), delta -> {}));
            truth(error.getMessage().contains("timed out"), "Deadline did not report a timeout");
            truth(client.cancelled.get(), "Timeout retained its HTTP resource");
        }
    }

    private static void consumerFailure() throws Exception {
        for (boolean anthropic : List.of(false, true)) {
            var client = new FixtureClient(fixture(anthropic, false), true, false, 200);
            var failure = new IOException("consumer closed");
            equal(expect(IOException.class, () -> provider(anthropic, client, TIMEOUT)
                    .stream(request(List.of()), new CancellationToken(), delta -> { throw failure; })), failure);
            truth(client.cancelled.get(), "Consumer failure left body open");
        }
    }

    private static void interrupt() throws Exception {
        for (boolean headers : List.of(false, true)) interrupt(headers);
    }

    private static void interrupt(boolean headers) throws Exception {
        var client = new FixtureClient(new byte[0], true, headers, 200);
        var interrupted = new AtomicBoolean();
        var failure = new AtomicReference<Throwable>();
        var worker = worker(() -> {
            try {
                provider(false, client, TIMEOUT).stream(request(List.of()), new CancellationToken(), delta -> {});
            } finally {
                interrupted.set(Thread.currentThread().isInterrupted());
            }
        }, failure);
        await(headers ? client.started : client.subscribed);
        worker.interrupt();
        join(worker);
        truth(failure.get() instanceof InterruptedException || failure.get() instanceof CancellationException,
                "Interrupt was not propagated: " + failure.get());
        truth(interrupted.get() && client.cancelled.get(), "Interrupt state or cleanup was lost");
    }

    private static void terminalWithoutEof() throws Exception {
        for (boolean anthropic : List.of(false, true)) {
            var client = new FixtureClient(fixture(anthropic, false), true, false, 200);
            var turn = provider(anthropic, client, TIMEOUT).stream(request(List.of()), new CancellationToken(), delta -> {});
            equal(turn.text(), "done\ncafé");
            truth(client.cancelled.get(), "Terminal event left unread response open");
        }
    }

    private static void httpErrorsAndOwnership() throws Exception {
        var client = new FixtureClient("denied".getBytes(StandardCharsets.UTF_8), false, false, 401);
        var provider = provider(false, client, TIMEOUT);
        var error = expect(ProviderException.class, () -> provider.stream(request(List.of()), new CancellationToken(), delta -> {}));
        truth(error.getMessage().contains("401: denied"), "HTTP status or detail was lost");
        provider.close();
        truth(!client.shutDown.get(), "Provider closed caller-owned client");
        try (var openai = new OpenAIProvider(); var anthropic = new AnthropicProvider()) {
            truth(openai != null && anthropic != null, "Default providers require credentials at construction");
        }
    }

    static Thread worker(Check action, AtomicReference<Throwable> failure) {
        return Thread.ofVirtual().start(() -> {
            try { action.run(); }
            catch (Throwable error) { failure.set(error); }
        });
    }

    static void await(CountDownLatch latch) throws InterruptedException {
        truth(latch.await(2, TimeUnit.SECONDS), "Timed out waiting for fixture");
    }

    static void join(Thread thread) throws InterruptedException {
        if (!thread.join(Duration.ofSeconds(2))) {
            thread.interrupt();
            throw new AssertionError("Provider did not release worker");
        }
    }

    static <T extends Throwable> T expect(Class<T> type, Check action) throws Exception {
        try { action.run(); }
        catch (Throwable error) {
            if (type.isInstance(error)) return type.cast(error);
            throw new AssertionError("Expected " + type.getSimpleName() + ", got " + error, error);
        }
        throw new AssertionError("Expected " + type.getSimpleName());
    }

    static void equal(Object actual, Object expected) {
        if (!actual.equals(expected)) throw new AssertionError("Expected " + expected + ", got " + actual);
    }

    static void truth(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }
}
