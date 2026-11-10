import dev.tinyagent.CancellationToken;
import dev.tinyagent.FixtureClient;
import dev.tinyagent.Message;
import dev.tinyagent.OpenAIProvider;
import dev.tinyagent.Provider;
import dev.tinyagent.ProviderException;
import dev.tinyagent.ProviderTurn;
import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.StringReader;
import java.net.URI;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.channels.Channels;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CancellationException;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/** Real SDK SSE parsing, shared Bash, typed history, and native CLI cleanup. */
public final class CliChecks {
    static final JsonMapper JSON = JsonMapper.builder().build();
    private CliChecks() {}

    public static void main(String[] args) throws Exception {
        success();
        System.out.println("PASS CLI streaming, replay, request schema, and shared Bash");
        sequential();
        System.out.println("PASS CLI sequential calls and recoverable argument errors");
        failure();
        System.out.println("PASS CLI malformed streams and provider failures preserve history");
        deliveryFailure();
        System.out.println("PASS CLI failed delivery and terminal cancellation preserve history");
        cancellation();
        System.out.println("PASS CLI cancellation releases pending headers, body, and Bash");
        console();
        System.out.println("PASS CLI conversation, rollback, blank input, and unterminated EOF");
        for (String mode : List.of("idle", "request", "bash", "stalled")) {
            shutdown(mode);
            System.out.println("PASS CLI native Ctrl-C during " + mode);
        }
        brokenOutput();
        System.out.println("PASS CLI broken output stops the provider");
        System.out.println("11 CLI groups passed");
    }

    static OpenAIProvider provider(FixtureClient... responses) {
        return new OpenAIProvider(new Sequence(responses), URI.create("http://fixture.invalid/responses"),
                "fixture-key", Duration.ofSeconds(10));
    }

    static FixtureClient response(String delta, JsonNode... output) {
        var completed = JSON.createObjectNode().put("type", "response.completed");
        var response = completed.putObject("response").put("status", "completed");
        var items = response.putArray("output");
        for (var item : output) items.add(item);
        String wire = "data: " + JSON.createObjectNode().put("type", "response.output_text.delta").put("delta", delta)
                + "\n\ndata: " + completed + "\n\n";
        return new FixtureClient(wire.getBytes(StandardCharsets.UTF_8), true, false, 200);
    }

    static JsonNode bash(String id, String command) {
        return call(id, JSON.createObjectNode().put("command", command));
    }

    private static JsonNode call(String id, JsonNode arguments) {
        return JSON.createObjectNode().put("type", "function_call").put("name", "bash").put("call_id", id)
                .put("arguments", arguments.toString());
    }

    static FixtureClient done() {
        var message = JSON.createObjectNode().put("type", "message").put("role", "assistant");
        message.putArray("content").addObject().put("type", "output_text").put("text", "done\ncafé");
        return response("done\ncafé", message);
    }

    private static List<Message> turn(Provider provider, List<Message> history, CancellationToken token) throws Exception {
        return TinyAgent.turn(provider, history, "run", token, value -> {}, value -> {});
    }

    private static void success() throws Exception {
        var terminalOnly = new StringBuilder();
        var completedText = JSON.createObjectNode().put("type", "message");
        completedText.putArray("content").addObject().put("type", "output_text").put("text", "terminal only");
        TinyAgent.turn(provider(response("", completedText)), List.of(), "run", new CancellationToken(), terminalOnly::append, value -> {});
        TinyAgent.check(terminalOnly.toString().equals("terminal only\n"), "Final text without deltas was hidden");
        for (String command : List.of("printf hello; printf problem >&2; exit 7", "rm forbidden", "yes x")) {
            var completed = done();
            var reasoning = JSON.createObjectNode().put("type", "reasoning").put("id", "reason-1").put("encrypted_content", "opaque");
            var first = response("working", reasoning, bash("call-1", command));
            var previous = new ArrayList<Message>();
            var text = new StringBuilder();
            var result = TinyAgent.turn(provider(first, completed), previous, "run", new CancellationToken(), text::append, value -> {});
            TinyAgent.check(previous.isEmpty() && result.size() == 4, "History committed before completion");
            TinyAgent.check(text.toString().equals("workingdone\ncafé\n"), "Deltas were lost or duplicated");
            TinyAgent.check(first.wasCancelled() && completed.wasCancelled(), "Completed streams retained their bodies");
            var request = JSON.readTree(completed.requestBytes());
            TinyAgent.check(request.path("stream").booleanValue() && !request.path("store").booleanValue()
                    && !request.path("parallel_tool_calls").booleanValue(), "CLI did not use the SDK request");
            TinyAgent.check(request.path("tools").get(0).path("parameters").path("required").get(0).stringValue().equals("command"),
                    "Bash metadata was lost");
            TinyAgent.check(request.path("input").get(1).equals(reasoning), "Opaque replay metadata was lost");
            String expected = switch (command) {
                case "rm forbidden" -> "exit_code: 126\nstdout:\n\nstderr:\nblocked: 'rm' is denied\n";
                case "yes x" -> "Tool failed: Tool output exceeds 131072 UTF-8 bytes";
                default -> "exit_code: 7\nstdout:\nhellostderr:\nproblem";
            };
            var observation = (Message.ToolResult) result.get(2);
            TinyAgent.check(expected.equals(observation.output())
                    && request.path("input").get(3).path("output").stringValue().equals(expected), "Wrong tool observation");
            TinyAgent.check(observation.isError() == command.equals("yes x"), "Wrong tool error flag");
        }
    }

    private static void sequential() throws Exception {
        var complete = done();
        var result = turn(provider(response("", bash("one", "printf first"), bash("two", "printf second")), complete),
                List.of(), new CancellationToken());
        TinyAgent.check(result.size() == 5 && result.get(2) instanceof Message.ToolResult first && first.callId().equals("one")
                && result.get(3) instanceof Message.ToolResult second && second.callId().equals("two"),
                "Tool calls did not execute sequentially");
        for (String raw : List.of("{}", "{\"command\":5}", "{\"command\":\"\"}", "{\"command\":\"printf unused\",\"extra\":1}")) {
            var history = turn(provider(response("", call("bad", JSON.readTree(raw))), done()), List.of(), new CancellationToken());
            TinyAgent.check(history.get(2) instanceof Message.ToolResult observation && observation.isError()
                    && observation.output().equals("Tool failed: Invalid arguments for tool 'bash'."), "Invalid arguments reached Bash");
        }
    }

    private static void failure() throws Exception {
        List<Message> previous = List.of(new Message.User("earlier"));
        for (String wire : List.of("", "data: {bad}\n\n",
                "data: {\"type\":\"response.output_text.delta\",\"delta\":\"partial\"}\n\n",
                "data: {\"type\":\"response.failed\"}\n\n")) {
            for (boolean afterTool : List.of(false, true)) {
                var broken = new FixtureClient(wire.getBytes(StandardCharsets.UTF_8));
                Provider selected = afterTool ? provider(response("", bash("one", "printf partial")), broken) : provider(broken);
                try {
                    turn(selected, previous, new CancellationToken());
                    throw new AssertionError("Failed turn returned history");
                } catch (ProviderException expected) {
                    TinyAgent.check(previous.equals(List.of(new Message.User("earlier"))), "Failed turn changed prior history");
                }
            }
        }
        try {
            turn(provider(new FixtureClient(new byte[0], false, false, 500)), previous, new CancellationToken());
            throw new AssertionError("HTTP failure returned history");
        } catch (ProviderException expected) {
            TinyAgent.check(expected.getMessage().contains("500"), "HTTP status was lost");
        }
    }

    private static void deliveryFailure() throws Exception {
        for (boolean diagnostic : List.of(false, true)) {
            var response = diagnostic ? response("", bash("one", "printf unused")) : done();
            var failure = new IOException("consumer closed");
            try {
                TinyAgent.turn(provider(response), List.of(), "run", new CancellationToken(),
                        value -> { if (!diagnostic) throw failure; },
                        value -> { if (diagnostic) throw failure; });
                throw new AssertionError("Failed delivery returned history");
            } catch (IOException expected) { TinyAgent.check(expected == failure, "Consumer failure changed"); }
            TinyAgent.check(response.wasCancelled(), "Consumer failure retained provider body");
        }
        var token = new CancellationToken();
        try {
            TinyAgent.turn(provider(done()), List.of(), "run", token,
                    value -> { if (value.equals("\n")) token.cancel(); }, value -> {});
            throw new AssertionError("Cancellation at terminal delivery returned history");
        } catch (CancellationException expected) { TinyAgent.check(token.isCancelled(), "Missing cancellation"); }
    }

    private static void cancellation() throws Exception {
        Path directory = Files.createTempDirectory(Path.of("target"), "cli-");
        Path pidFile = directory.resolve("pid");
        try {
            for (String mode : List.of("headers", "body", "bash")) {
                var ready = new CountDownLatch(1);
                var client = mode.equals("bash") ? response("", bash("one", waitCommand(pidFile)))
                        : new FixtureClient(new byte[0], mode.equals("body"), mode.equals("headers"), 200) {
                            @Override public <T> CompletableFuture<HttpResponse<T>> sendAsync(
                                    HttpRequest request, HttpResponse.BodyHandler<T> handler) {
                                var result = super.sendAsync(request, handler);
                                ready.countDown();
                                return result;
                            }
                        };
                var token = new CancellationToken();
                var failure = new AtomicReference<Throwable>();
                var previous = new ArrayList<Message>();
                var running = Thread.ofVirtual().start(() -> {
                    try { turn(provider(client), previous, token); }
                    catch (Throwable error) { failure.set(error); }
                });
                try {
                    long pid = mode.equals("bash") ? readPid(pidFile) : 0;
                    if (pid == 0) TinyAgent.check(ready.await(2, TimeUnit.SECONDS), "CLI request did not start");
                    token.cancel();
                    TinyAgent.check(running.join(Duration.ofSeconds(2)), "CLI ignored cancellation");
                    TinyAgent.check(failure.get() instanceof CancellationException, "Cancellation did not propagate: " + failure.get());
                    TinyAgent.check(previous.isEmpty() && client.wasCancelled(), "Cancellation retained history or HTTP work");
                    if (pid != 0) gone(pid);
                } finally {
                    token.cancel();
                    TinyAgent.check(running.join(Duration.ofSeconds(2)), "CLI cleanup did not finish");
                }
            }
        } finally {
            Files.deleteIfExists(pidFile);
            Files.delete(directory);
        }
    }

    private static void console() throws Exception {
        var counts = new ArrayList<Integer>();
        Provider provider = (request, token, text) -> {
            counts.add(request.history().size());
            if (counts.size() == 2) throw new ProviderException("fixture failed");
            text.accept("done");
            return new ProviderTurn("done", List.of(), List.of(), List.of());
        };
        var output = new ByteArrayOutputStream();
        var error = new ByteArrayOutputStream();
        TinyAgent.run(provider, new BufferedReader(new StringReader("\nfirst\nfail\nlast")),
                Channels.newChannel(output), Channels.newChannel(error));
        TinyAgent.check(counts.equals(List.of(1, 3, 3)), "Console did not preserve committed history through failure");
        TinyAgent.check(output.toString(StandardCharsets.UTF_8).contains("> done")
                && error.toString(StandardCharsets.UTF_8).contains("fixture failed"), "Missing console output");
    }

    private static void shutdown(String mode) throws Exception {
        Path directory = Files.createTempDirectory(Path.of("target"), "cli-");
        Path marker = directory.resolve("marker").toAbsolutePath();
        var process = child(mode, marker);
        try {
            long pid = readPid(marker);
            if (mode.equals("stalled")) {
                long end = System.nanoTime() + Duration.ofSeconds(2).toNanos();
                while (process.getInputStream().available() < 8192 && process.isAlive() && System.nanoTime() < end) Thread.sleep(10);
                TinyAgent.check(process.getInputStream().available() >= 8192, "CLI output never filled its pipe");
            }
            var signal = new ProcessBuilder("/bin/kill", "-INT", Long.toString(process.pid())).start();
            TinyAgent.check(signal.waitFor(2, TimeUnit.SECONDS) && signal.exitValue() == 0, "Could not signal CLI");
            TinyAgent.check(process.waitFor(3, TimeUnit.SECONDS), "JVM shutdown did not finish during " + mode);
            TinyAgent.check(process.exitValue() != 0, "Shutdown unexpectedly returned success");
            if (mode.equals("bash")) gone(pid);
            if (mode.equals("request") || mode.equals("stalled"))
                TinyAgent.check(Files.readString(marker).equals("2"), "JVM exited before provider cleanup");
        } finally { reap(process); Files.deleteIfExists(marker); Files.delete(directory); }
    }

    private static void brokenOutput() throws Exception {
        Path directory = Files.createTempDirectory(Path.of("target"), "cli-");
        Path marker = directory.resolve("marker").toAbsolutePath();
        var process = child("stalled", marker);
        try {
            readPid(marker);
            process.getInputStream().close();
            TinyAgent.check(process.waitFor(3, TimeUnit.SECONDS), "Broken CLI output retained the provider");
            TinyAgent.check(process.exitValue() != 0, "Broken CLI output returned success");
            TinyAgent.check(Files.readString(marker).equals("2"), "Broken output skipped provider cleanup");
        } finally { reap(process); Files.deleteIfExists(marker); Files.delete(directory); }
    }

    private static Process child(String mode, Path marker) throws IOException {
        return new ProcessBuilder(Path.of(System.getProperty("java.home"), "bin/java").toString(), "-cp",
                System.getProperty("java.class.path"), "CliFixture", mode, marker.toString())
                .redirectError(ProcessBuilder.Redirect.DISCARD).start();
    }

    private static void reap(Process process) throws Exception {
        process.getOutputStream().close();
        if (process.isAlive()) {
            process.destroy();
            if (!process.waitFor(3, TimeUnit.SECONDS)) process.destroyForcibly().waitFor();
        }
        process.getInputStream().close();
        process.getErrorStream().close();
    }

    static String waitCommand(Path file) {
        return "sleep 30 & child=$!; printf '%s' \"$child\" > '"
                + file.toAbsolutePath().toString().replace("'", "'\\''") + "'; wait";
    }

    private static long readPid(Path file) throws Exception {
        long end = System.nanoTime() + Duration.ofSeconds(3).toNanos();
        while (System.nanoTime() < end) {
            if (Files.exists(file)) {
                String text = Files.readString(file);
                if (!text.isEmpty()) return Long.parseLong(text);
            }
            Thread.sleep(10);
        }
        throw new AssertionError("CLI fixture did not report readiness");
    }

    private static void gone(long pid) throws Exception {
        long end = System.nanoTime() + Duration.ofSeconds(2).toNanos();
        while (System.nanoTime() < end) {
            if (ProcessHandle.of(pid).isEmpty()) return;
            Thread.sleep(10);
        }
        throw new AssertionError("CLI Bash child is still present: " + pid);
    }

    private static final class Sequence extends FixtureClient {
        private final List<FixtureClient> responses;
        private int next;

        Sequence(FixtureClient... responses) { super(new byte[0]); this.responses = List.of(responses); }

        @Override
        public <T> CompletableFuture<HttpResponse<T>> sendAsync(HttpRequest request, HttpResponse.BodyHandler<T> handler) {
            return responses.get(next++).sendAsync(request, handler);
        }
    }
}
