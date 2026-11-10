package dev.tinyagent;

import static dev.tinyagent.ProviderChecks.check;
import static dev.tinyagent.ProviderChecks.equal;
import static dev.tinyagent.ProviderChecks.expect;
import static dev.tinyagent.ProviderChecks.truth;

import java.io.BufferedReader;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.ByteBuffer;
import java.nio.channels.Channels;
import java.nio.channels.Pipe;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

final class RpcChecks {
    private RpcChecks() {}

    static void run() {
        check("RPC shared idle and invalid command fixtures", RpcChecks::sharedCommands);
        check("RPC subprocess completion, history, and rollback", RpcChecks::history);
        check("RPC subprocess tools, recovery, and cancellation rollback", RpcChecks::tools);
        check("RPC subprocess busy, abort, and reset", RpcChecks::cancelAndReset);
        check("RPC strict JSON and UTF-8 framing", RpcChecks::framing);
        check("RPC input byte limit and EOF framing", RpcChecks::inputBounds);
        check("RPC oversized output cancels and permits reuse", RpcChecks::oversizedOutput);
        check("RPC active EOF and Ctrl-C cleanup", RpcChecks::shutdown);
        check("RPC stalled stdout exits after EOF", RpcChecks::stalledOutput);
        check("RPC cancelled output drains before connection reuse", RpcChecks::cancelledDrain);
        check("RPC broken output cancels and joins native work", RpcChecks::brokenOutput);
        check("RPC bounded queue preserves the current frame", RpcChecks::queueBounds);
        check("RPC subprocess queue overflow shuts down", RpcChecks::queueOverflow);
        check("RPC imported channel ownership and interruption", RpcChecks::channels);
    }

    private static ObjectNode command(String id, String type) {
        return Json.object(type).put("id", id);
    }

    private static ObjectNode prompt(String id, String message) {
        return command(id, "prompt").put("message", message);
    }

    private static void sharedCommands() throws Exception {
        var fixture = Json.parse(Files.readString(Path.of("../../conformance/rpc_v1.json")));
        equal(fixture.path("limits").path("jsonlLineBytes").intValue(), RpcProtocol.MAX_LINE_BYTES);
        equal(fixture.path("limits").path("queuedOutputBytes").intValue(), RpcProtocol.MAX_QUEUE_BYTES);
        try (var child = new Child(true)) {
            for (var scenario : fixture.path("scenarios")) {
                if (!scenario.has("expected")) continue;
                for (var request : scenario.path("commands")) child.send(request);
                for (var expected : scenario.path("expected")) equal(child.next(), expected);
            }
            child.send(command("", "get_state"));
            equal(child.next().path("id").stringValue(), "");
            child.eof();
            child.exit(0);
        }
    }

    private static void history() throws Exception {
        try (var child = new Child(true)) {
            child.send(prompt("first", "finish"));
            equal(child.run("first", "completed").path("text").stringValue(), "done\ncafé");
            equal(child.idle().path("messageCount").intValue(), 2);
            child.send(prompt("second", "history"));
            equal(child.run("second", "completed").path("text").stringValue(), "messages=3");
            equal(child.idle().path("messageCount").intValue(), 4);
            child.send(prompt("failure", "fail"));
            child.run("failure", "failed");
            equal(child.idle().path("messageCount").intValue(), 4);
            child.eof();
            child.exit(0);
        }
    }

    private static void tools() throws Exception {
        try (var child = new Child(true)) {
            for (String message : List.of("tool", "tool_invalid")) {
                child.send(prompt("go", message));
                var ack = child.next();
                equal(ack.path("id").stringValue(), "go");
                String id = ack.path("data").path("runId").stringValue();
                equal(child.next().path("type").stringValue(), "agent_start");
                var start = child.next();
                equal(start.path("type").stringValue(), "tool_execution_start");
                equal(start.path("arguments").path("text").stringValue(), message.equals("tool") ? message : "");
                var end = child.next();
                equal(end.path("type").stringValue(), "tool_execution_end");
                equal(end.path("toolCallId"), start.path("toolCallId"));
                equal(end.path("isError").booleanValue(), message.equals("tool_invalid"));
                equal(child.next().path("delta"), end.path("output"));
                var terminal = child.next();
                equal(terminal.path("status").stringValue(), "completed");
                equal(terminal.path("sequence").intValue(), 5);
                equal(terminal.path("runId").stringValue(), id);
            }
            equal(child.idle().path("messageCount").intValue(), 8);
            child.send(prompt("held", "tool_wait"));
            child.next();
            equal(child.next().path("type").stringValue(), "agent_start");
            equal(child.next().path("type").stringValue(), "tool_execution_start");
            child.errorContains("tool-waiting");
            child.send(command("cancel", "abort"));
            int sequence = 2;
            boolean acknowledged = false;
            for (int index = 0; index < 3; index++) {
                var value = child.next();
                if (value.path("type").stringValue().equals("response")) {
                    equal(value.path("id").stringValue(), "cancel");
                    truth(value.path("success").booleanValue(), "Abort failed");
                    acknowledged = true;
                } else {
                    equal(value.path("sequence").intValue(), ++sequence);
                    if (sequence == 3) {
                        equal(value.path("type").stringValue(), "tool_execution_end");
                        truth(value.path("isError").booleanValue(), "Cancelled tool reported success");
                    } else equal(value.path("status").stringValue(), "aborted");
                }
            }
            truth(acknowledged, "Missing abort acknowledgement");
            child.errorContains("tool-cleaned");
            equal(child.idle().path("messageCount").intValue(), 8);
            child.send(prompt("next", "history"));
            equal(child.run("next", "completed").path("text").stringValue(), "messages=9");
            child.eof();
            child.exit(0);
        }
    }

    private static void cancelAndReset() throws Exception {
        for (String action : List.of("abort", "new_session")) {
            try (var child = new Child(true)) {
                child.send(prompt("held", "wait"));
                String id = child.next().path("data").path("runId").stringValue();
                equal(child.next().path("type").stringValue(), "agent_start");
                child.errorContains("provider-waiting");
                child.send(prompt("busy", "second"));
                equal(child.next().path("error").path("code").stringValue(), "busy");
                child.send(command("cancel", action));
                var first = child.next();
                var second = child.next();
                var response = first.path("type").stringValue().equals("response") ? first : second;
                var terminal = response == first ? second : first;
                equal(response.path("id").stringValue(), "cancel");
                truth(response.path("success").booleanValue(), "Cancellation command failed");
                equal(terminal.path("status").stringValue(), "aborted");
                equal(terminal.path("runId").stringValue(), id);
                child.errorContains("provider-cleaned");
                equal(child.idle().path("messageCount").intValue(), 0);
                child.send(prompt("next", "finish"));
                child.run("next", "completed");
                child.eof();
                child.exit(0);
            }
        }
    }

    private static void framing() throws Exception {
        try (var child = new Child(true)) {
            for (String line : List.of("{", "{} {}", "NaN", "{\"extra\":\"\\ud800\"}", "{\"\\ud800\":0}")) {
                child.raw((line + "\n").getBytes(StandardCharsets.UTF_8));
                equal(child.next().path("error").path("code").stringValue(), "invalid_json");
            }
            child.raw("{\"id\":\"café\",\"type\":\"get_state\"}\r\n".getBytes(StandardCharsets.UTF_8));
            equal(child.next().path("id").stringValue(), "café");
            child.raw(new byte[] {(byte) 0xff, '\n'});
            equal(child.next().path("error").path("code").stringValue(), "invalid_utf8");
            child.exit(0);
        }
    }

    private static void inputBounds() throws Exception {
        try (var child = new Child(true)) {
            String line = command("exact", "get_state").toString();
            child.raw((line + " ".repeat(RpcProtocol.MAX_LINE_BYTES - line.length()) + "\n").getBytes(StandardCharsets.UTF_8));
            equal(child.next().path("id").stringValue(), "exact");
            child.raw(command("eof", "get_state").toString().getBytes(StandardCharsets.UTF_8));
            child.eof();
            equal(child.next().path("id").stringValue(), "eof");
            child.exit(0);
        }
        try (var child = new Child(true)) {
            child.raw(" ".repeat(RpcProtocol.MAX_LINE_BYTES + 1).getBytes(StandardCharsets.UTF_8));
            equal(child.next().path("error").path("code").stringValue(), "line_too_large");
            child.exit(0);
        }
    }

    private static void oversizedOutput() throws Exception {
        try (var child = new Child(true)) {
            child.send(prompt("large", "large"));
            equal(child.next().path("id").stringValue(), "large");
            equal(child.next().path("type").stringValue(), "agent_start");
            equal(child.next().path("error").path("code").stringValue(), "output_too_large");
            equal(child.idle().path("messageCount").intValue(), 0);
            child.send(prompt("next", "finish"));
            child.run("next", "completed");
            child.eof();
            child.exit(0);
        }
    }

    private static void shutdown() throws Exception {
        for (boolean signal : List.of(false, true)) {
            try (var child = new Child(true)) {
                child.send(prompt("held", "wait"));
                child.next();
                child.next();
                child.errorContains("provider-waiting");
                if (signal) {
                    var kill = new ProcessBuilder("/bin/kill", "-INT", Long.toString(child.process.pid())).start();
                    truth(kill.waitFor(2, TimeUnit.SECONDS) && kill.exitValue() == 0, "Could not signal fixture process");
                } else child.eof();
                equal(child.next().path("status").stringValue(), "aborted");
                child.exit(signal ? 130 : 0);
                child.errorContains("provider-cleaned");
            }
        }
    }

    private static void stalledOutput() throws Exception {
        try (var child = new Child(false)) {
            child.send(prompt("x".repeat(600_000), "finish"));
            child.eof();
            child.exit(1);
            truth(child.errors().contains("RPC failed"), "Stalled output did not report a failure");
        }
    }

    private static void cancelledDrain() throws Exception {
        try (var child = new Child(false)) {
            String id = "x".repeat(200_000);
            child.send(prompt(id, "wait"));
            child.send(command("abort", "abort"));
            child.startReading();
            int acknowledgements = 0;
            int events = 0;
            for (int index = 0; index < 4; index++) {
                var value = child.next();
                if (value.path("type").stringValue().equals("response")) {
                    acknowledgements++;
                    truth(value.path("success").booleanValue(), "Cancelled acknowledgement failed");
                } else {
                    equal(value.path("sequence").intValue(), ++events);
                    if (events == 2) equal(value.path("status").stringValue(), "aborted");
                }
            }
            equal(acknowledgements, 2);
            equal(events, 2);
            equal(child.idle().path("messageCount").intValue(), 0);
            child.send(prompt("next", "finish"));
            child.run("next", "completed");
            child.eof();
            child.exit(0);
        }
    }

    private static void brokenOutput() throws Exception {
        var input = Pipe.open();
        var output = Pipe.open();
        var entered = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        var cleaned = new AtomicBoolean();
        var failure = new AtomicReference<Throwable>();
        var agent = new Agent((request, token, text) -> {
            entered.countDown();
            try {
                ProviderChecks.await(release);
                text.accept("write after disconnect");
                throw new AssertionError("Broken output was accepted");
            } finally { cleaned.set(true); }
        }, "fixture", "");
        var task = Thread.ofPlatform().start(() -> {
            try { Rpc.run(agent, input.source(), output.sink()); }
            catch (Exception error) { failure.set(error); }
        });
        try {
            input.sink().write(ByteBuffer.wrap((prompt("go", "go") + "\n").getBytes(StandardCharsets.UTF_8)));
            ProviderChecks.await(entered);
            output.source().close();
            release.countDown();
            ProviderChecks.join(task);
            truth(failure.get() instanceof IOException, "Broken output did not propagate");
            truth(cleaned.get(), "RPC returned before provider cleanup");
            truth(!input.source().isOpen() && !output.sink().isOpen(), "Failed RPC retained channels");
        } finally {
            release.countDown();
            input.sink().close();
            output.source().close();
            task.interrupt();
            ProviderChecks.join(task);
        }
    }

    private static void queueBounds() throws Exception {
        var output = new RpcOutput();
        var value = Json.object("fixture").put("text", "x".repeat(200_000));
        output.emit(value, null);
        var current = output.next();
        current.bytes.position(17);
        RpcException overflow = null;
        for (int index = 0; index < 30; index++) {
            try { output.emit(value, null); }
            catch (RpcException error) { overflow = error; break; }
        }
        truth(overflow != null, "Unbounded output queue accepted all frames");
        equal(overflow.code(), "output_queue_full");
        equal(current.bytes.position(), 17);
        output.complete(current);
        var last = output.next();
        equal(Json.MAPPER.readTree(last.bytes.array()).path("error").path("code").stringValue(), "output_queue_full");
        output.complete(last);
        truth(output.next() == null, "Overflow retained pending lines");
        expect(RpcException.class, () -> output.emit(value, null));
    }

    private static void queueOverflow() throws Exception {
        try (var child = new Child(false)) {
            var feeder = Thread.ofPlatform().start(() -> {
                try {
                    for (int index = 0; index < 80; index++) child.send(command("x".repeat(100_000), "unknown"));
                } catch (IOException ignored) { /* The transport closes input when its queue fills. */ }
            });
            try {
                child.exit(1);
                truth(child.errors().contains("queue"), "Queue overflow was not reported");
            } finally {
                child.process.destroyForcibly();
                ProviderChecks.join(feeder);
            }
        }
    }

    private static void channels() throws Exception {
        var input = Channels.newChannel(new ByteArrayInputStream((command("state", "get_state") + "\n").getBytes(StandardCharsets.UTF_8)));
        var bytes = new ByteArrayOutputStream();
        var output = Channels.newChannel(bytes);
        var agent = new Agent((request, token, text) -> { throw new AssertionError("Idle RPC called provider"); }, "fixture", "");
        Rpc.run(agent, input, output);
        truth(!input.isOpen() && !output.isOpen(), "RPC did not close its owned channels");
        equal(Json.parse(bytes.toString(StandardCharsets.UTF_8)).path("id").stringValue(), "state");

        var pipe = Pipe.open();
        var sink = Channels.newChannel(new ByteArrayOutputStream());
        var failure = new AtomicReference<Throwable>();
        var task = Thread.ofPlatform().start(() -> {
            try { Rpc.run(agent, pipe.source(), sink); }
            catch (Exception error) { failure.set(error); }
        });
        try {
            task.interrupt();
            ProviderChecks.join(task);
            truth(failure.get() instanceof InterruptedException, "Caller interruption did not propagate");
            truth(!pipe.source().isOpen() && !sink.isOpen(), "Interrupted RPC retained channels");
        } finally { pipe.sink().close(); pipe.source().close(); }
    }

    private static final class Child implements AutoCloseable {
        private final Process process;
        private final LinkedBlockingQueue<JsonNode> lines = new LinkedBlockingQueue<>();
        private final AtomicReference<Throwable> readFailure = new AtomicReference<>();
        private final StringBuilder errors = new StringBuilder();
        private Thread stdout;
        private final Thread stderr;

        Child(boolean readOutput) throws IOException {
            var builder = new ProcessBuilder(System.getProperty("java.home") + "/bin/java", "-cp",
                    System.getProperty("java.class.path"), "dev.tinyagent.RpcFixture");
            builder.environment().remove("OPENAI_API_KEY");
            builder.environment().remove("ANTHROPIC_API_KEY");
            process = builder.start();
            stderr = Thread.ofPlatform().start(() -> {
                try (var reader = new BufferedReader(new InputStreamReader(process.getErrorStream(), StandardCharsets.UTF_8))) {
                    String line;
                    while ((line = reader.readLine()) != null) synchronized (errors) { errors.append(line).append('\n'); }
                } catch (IOException error) { readFailure.compareAndSet(null, error); }
            });
            if (readOutput) startReading();
        }

        void startReading() {
            if (stdout != null) throw new IllegalStateException("Already reading output");
            stdout = Thread.ofPlatform().start(() -> {
                try (var reader = new BufferedReader(new InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8))) {
                    String line;
                    while ((line = reader.readLine()) != null) {
                        truth(line.getBytes(StandardCharsets.UTF_8).length <= RpcProtocol.MAX_LINE_BYTES, "Oversized output line");
                        lines.add(Json.parse(line));
                    }
                } catch (Exception | AssertionError error) { readFailure.compareAndSet(null, error); }
            });
        }

        void send(JsonNode value) throws IOException { raw((value + "\n").getBytes(StandardCharsets.UTF_8)); }
        void raw(byte[] bytes) throws IOException { process.getOutputStream().write(bytes); process.getOutputStream().flush(); }
        void eof() throws IOException { process.getOutputStream().close(); }
        String errors() { synchronized (errors) { return errors.toString(); } }

        JsonNode next() throws Exception {
            var value = lines.poll(4, TimeUnit.SECONDS);
            truth(value != null, "Missing RPC output: " + readFailure.get() + " " + errors());
            return value;
        }

        ObjectNode run(String commandId, String status) throws Exception {
            var ack = next();
            equal(ack.path("id").stringValue(), commandId);
            truth(ack.path("success").booleanValue(), "Prompt acknowledgement failed");
            String id = ack.path("data").path("runId").stringValue();
            long sequence = 0;
            for (;;) {
                var event = next();
                equal(event.path("runId").stringValue(), id);
                equal(event.path("sequence").longValue(), ++sequence);
                if (sequence == 1) equal(event.path("type").stringValue(), "agent_start");
                if (event.path("type").stringValue().equals("agent_end")) {
                    equal(event.path("status").stringValue(), status);
                    return (ObjectNode) event;
                }
            }
        }

        JsonNode idle() throws Exception {
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(3);
            do {
                send(command("state", "get_state"));
                var state = next().path("data");
                if (state.path("status").stringValue().equals("idle")) return state;
                Thread.sleep(5);
            } while (System.nanoTime() < deadline);
            throw new AssertionError("Session did not become idle");
        }

        void errorContains(String text) throws Exception {
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(3);
            while (!errors().contains(text) && System.nanoTime() < deadline) Thread.sleep(5);
            truth(errors().contains(text), "Missing fixture marker " + text + ": " + errors());
        }

        void exit(int expected) throws Exception {
            truth(process.waitFor(5, TimeUnit.SECONDS), "RPC subprocess did not exit: " + errors());
            ProviderChecks.join(stderr);
            if (stdout != null) ProviderChecks.join(stdout);
            equal(process.exitValue(), expected);
            truth(readFailure.get() == null, "Invalid RPC output: " + readFailure.get());
        }

        @Override public void close() {
            if (process.isAlive()) process.destroyForcibly();
            try {
                process.waitFor(3, TimeUnit.SECONDS);
                process.getOutputStream().close();
                process.getInputStream().close();
                stderr.join(1000);
                if (stdout != null) stdout.join(1000);
            } catch (IOException ignored) {
                // A fixture may deliberately close its pipe first.
            } catch (InterruptedException error) { Thread.currentThread().interrupt(); }
        }
    }
}
