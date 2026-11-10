package dev.tinyagent;

import static dev.tinyagent.ProviderChecks.await;
import static dev.tinyagent.ProviderChecks.check;
import static dev.tinyagent.ProviderChecks.equal;
import static dev.tinyagent.ProviderChecks.expect;
import static dev.tinyagent.ProviderChecks.join;
import static dev.tinyagent.ProviderChecks.truth;
import static dev.tinyagent.ProviderChecks.worker;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CancellationException;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

final class ToolChecks {
    private ToolChecks() {}

    static void run() {
        check("Tools shared schemas and rejected construction", ToolChecks::schemas);
        check("Tools immutable arguments, metadata, and annotations", ToolChecks::snapshots);
        check("Tools native providers and replay", ToolChecks::nativeProviders);
        check("Tools sequential bounded recovery and ordered events", ToolChecks::recovery);
        check("Tools unknown names reject the whole batch", ToolChecks::unknownBatch);
        check("Tools cancellation pairs events and scopes tokens", ToolChecks::cancellation);
        check("Tools cancellation and interruption join held cleanup", ToolChecks::heldCleanup);
        check("Tools consumer failures and retained callbacks", ToolChecks::callbackOwnership);
        check("Tools RPC acknowledgement, commit, and rollback", ToolChecks::session);
    }

    private static Tool echo(Tool.Handler handler) throws Exception {
        return new Tool("echo", "Return text", Json.parse("""
                {"type":"object","properties":{"text":{"type":"string","minLength":1}},
                 "required":["text"],"additionalProperties":false}
                """), handler);
    }

    private static ToolCall call(String id, JsonNode arguments) { return new ToolCall(id, "echo", arguments); }

    private static Provider rounds(List<ToolCall> calls, ProviderChecks.Check after) {
        var round = new AtomicInteger();
        return (request, token, text) -> {
            if (round.getAndIncrement() == 0) return new ProviderTurn("working", calls, List.of(), List.of());
            after.run();
            return CoreChecks.turn("done");
        };
    }

    private static void schemas() throws Exception {
        var fixture = Json.parse(Files.readString(Path.of("../../conformance/tool_arguments.json")));
        for (var value : fixture.path("cases")) {
            var called = new AtomicInteger();
            var tool = new Tool("echo", "", value.get("schema"), (arguments, token) -> { called.incrementAndGet(); return "ok"; });
            for (var input : value.get("valid")) equal(tool.call((ObjectNode) input, new CancellationToken()), "ok");
            for (var input : value.get("invalid")) {
                expect(IllegalArgumentException.class, () -> tool.call((ObjectNode) input, new CancellationToken()));
            }
            equal(called.get(), value.get("valid").size());
        }
        for (String json : List.of(
                "{\"type\":\"bad\"}", "{\"properties\":[]}", "{\"required\":\"text\"}", "{\"required\":[\"x\",\"x\"]}",
                "{\"$defs\":{\"unused\":{\"type\":\"bad\"}}}", "{\"properties\":{\"unused\":{\"$ref\":\"https://invalid.example\"}}}",
                "{\"$ref\":\"#/$defs/missing\"}", "{\"$ref\":\"#\"}", "{\"allOf\":[{\"$ref\":\"#\"}]}",
                "{\"$defs\":{\"unused\":{\"$ref\":\"#/$defs/unused\"}}}", "{\"$dynamicAnchor\":\"node\",\"$dynamicRef\":\"#node\"}",
                "{\"$schema\":\"http://json-schema.org/draft-07/schema#\"}", "{\"$schema\":\"https://invalid.example\"}")) {
            expect(RuntimeException.class, () -> new Tool("echo", "", Json.parse(json), (arguments, token) -> "ok"));
        }
        var allow = new Tool("allow", "", Json.parse("true"), (arguments, token) -> "ok");
        equal(allow.call(Json.MAPPER.createObjectNode(), new CancellationToken()), "ok");
        var deny = new Tool("deny", "", Json.parse("false"), (arguments, token) -> "wrong");
        expect(IllegalArgumentException.class, () -> deny.call(Json.MAPPER.createObjectNode(), new CancellationToken()));
    }

    private static void snapshots() throws Exception {
        var schema = (ObjectNode) Json.parse("""
                {"type":"object","properties":{"n":{"type":"integer","default":4},"text":{"format":"email"}},
                 "examples":[{"$ref":"https://annotation.invalid"}]}
                """);
        var received = new AtomicReference<ObjectNode>();
        var tool = new Tool("echo", "", schema, (arguments, token) -> {
            received.set(arguments.deepCopy());
            arguments.put("changed", true);
            return "ok";
        });
        ((ObjectNode) schema.path("properties").path("n")).put("type", "string");
        ((ObjectNode) tool.definition().parameters().path("properties").path("n")).put("type", "boolean");
        var arguments = Json.MAPPER.createObjectNode().put("text", "not an email");
        equal(tool.call(arguments, new CancellationToken()), "ok");
        equal(received.get(), arguments);
        truth(!received.get().has("n"), "Validator inserted a default");
        expect(IllegalArgumentException.class, () -> tool.call(Json.MAPPER.createObjectNode().put("n", "4"), new CancellationToken()));
        expect(IllegalArgumentException.class, () -> new Agent(rounds(List.of(), () -> {}), "fixture", "", List.of(tool, tool)));
        var event = new AgentEvent.ToolStarted("run", 1, "call", "echo", arguments);
        event.arguments().put("text", "changed");
        ((ObjectNode) event.toJson().path("arguments")).put("text", "changed");
        equal(event.arguments(), arguments);
    }

    private static void nativeProviders() throws Exception {
        for (boolean anthropic : List.of(false, true)) {
            var first = new FixtureClient(ProviderChecks.fixture(anthropic, true), true, false, 200);
            var second = new FixtureClient(ProviderChecks.fixture(anthropic, false), true, false, 200);
            var round = new AtomicInteger();
            Provider provider = (request, token, text) -> ProviderChecks.provider(anthropic,
                    round.getAndIncrement() == 0 ? first : second, ProviderChecks.TIMEOUT).stream(request, token, text);
            var tool = echo((arguments, token) -> {
                truth(first.wasCancelled(), "Tool started before provider cleanup");
                equal(arguments.path("text").stringValue(), "café");
                return "native café";
            });
            var events = new ArrayList<AgentEvent>();
            var result = new Agent(provider, "fixture", "", List.of(tool)).run("go", List.of(), new CancellationToken(), events::add);
            equal(result.history().size(), 4);
            equal(((Message.ToolResult) result.history().get(2)).output(), "native café");
            equal(events.stream().filter(AgentEvent.ToolStarted.class::isInstance).count(), 1L);
            equal(events.stream().filter(AgentEvent.ToolEnded.class::isInstance).count(), 1L);
            truth(second.wasCancelled(), "Final provider response remained open");
            var request = Json.MAPPER.readTree(second.requestBytes());
            equal(request.path("tools").get(0).path("name").stringValue(), "echo");
            if (anthropic) {
                equal(request.path("messages").get(1).path("content").get(0).path("signature").stringValue(), "signed-fixture");
                equal(request.path("messages").get(2).path("content").get(0).path("content").stringValue(), "native café");
            } else {
                equal(request.path("input").get(1).path("encrypted_content").stringValue(), "opaque-fixture");
                equal(request.path("input").get(3).path("output").stringValue(), "native café");
            }
        }
    }

    private static void recovery() throws Exception {
        var handled = new ArrayList<String>();
        var tool = echo((arguments, token) -> {
            String text = arguments.path("text").stringValue();
            handled.add(text);
            return switch (text) {
                case "throw" -> throw new IOException("é".repeat(4000));
                case "large" -> "é".repeat(Tool.MAX_OUTPUT_BYTES);
                case "invalid" -> "\ud800";
                default -> text;
            };
        });
        var calls = new ArrayList<ToolCall>();
        for (String text : List.of("first", "throw", "large", "invalid", "", "last")) {
            calls.add(call("call-" + calls.size(), Json.MAPPER.createObjectNode().put("text", text)));
        }
        var events = new ArrayList<AgentEvent>();
        var result = new Agent(rounds(calls, () -> {}), "fixture", "", List.of(tool))
                .run("go", List.of(), new CancellationToken(), events::add);
        equal(handled, List.of("first", "throw", "large", "invalid", "last"));
        var observations = result.history().stream().filter(Message.ToolResult.class::isInstance).map(Message.ToolResult.class::cast).toList();
        equal(observations.stream().map(Message.ToolResult::isError).toList(), List.of(false, true, true, true, true, false));
        for (var observation : observations) {
            truth(Json.validText(observation.output()), "Invalid output escaped");
            truth(observation.output().getBytes(StandardCharsets.UTF_8).length <= Tool.MAX_OUTPUT_BYTES, "Error observation too large");
        }
        var toolEvents = events.stream().filter(event -> event instanceof AgentEvent.ToolStarted || event instanceof AgentEvent.ToolEnded).toList();
        equal(toolEvents.size(), calls.size() * 2);
        for (int index = 0; index < calls.size(); index++) {
            equal(((AgentEvent.ToolStarted) toolEvents.get(index * 2)).toolCallId(), calls.get(index).callId());
            equal(((AgentEvent.ToolEnded) toolEvents.get(index * 2 + 1)).toolCallId(), calls.get(index).callId());
        }
        equal(events.stream().filter(AgentEvent.TextDelta.class::isInstance).map(AgentEvent.TextDelta.class::cast)
                .map(AgentEvent.TextDelta::delta).toList(), List.of("working", "done"));
        for (int index = 0; index < events.size(); index++) {
            equal(events.get(index).sequence(), (long) index + 1);
            equal(events.get(index).runId(), events.getFirst().runId());
        }
        truth(events.getLast() instanceof AgentEvent.Completed, "Missing completion");
    }

    private static void unknownBatch() throws Exception {
        var entered = new AtomicInteger();
        var tool = echo((arguments, token) -> { entered.incrementAndGet(); return "ok"; });
        var calls = List.of(call("1", Json.MAPPER.createObjectNode().put("text", "ok")),
                new ToolCall("2", "missing", Json.MAPPER.createObjectNode()));
        var events = new ArrayList<AgentEvent>();
        expect(ProviderException.class, () -> new Agent(rounds(calls, () -> {}), "fixture", "", List.of(tool))
                .run("go", List.of(), new CancellationToken(), events::add));
        equal(entered.get(), 0);
        equal(events.stream().filter(AgentEvent.ToolStarted.class::isInstance).count(), 0L);
        truth(events.getLast() instanceof AgentEvent.Failed, "Unknown tool did not fail the run");
    }

    private static void cancellation() throws Exception {
        for (String phase : List.of("start", "handler", "end")) {
            var parent = new CancellationToken();
            var entered = new AtomicInteger();
            var events = new ArrayList<AgentEvent>();
            var tool = echo((arguments, token) -> {
                entered.incrementAndGet();
                if (phase.equals("handler")) { token.cancel(); throw new IOException("wrapped cancellation"); }
                return "ok";
            });
            var provider = rounds(List.of(call("1", Json.MAPPER.createObjectNode().put("text", "ok"))),
                    () -> { throw new AssertionError("Provider continued after cancellation"); });
            expect(CancellationException.class, () -> new Agent(provider, "fixture", "", List.of(tool))
                    .run("go", List.of(), parent, event -> {
                        events.add(event);
                        if ((phase.equals("start") && event instanceof AgentEvent.ToolStarted)
                                || (phase.equals("end") && event instanceof AgentEvent.ToolEnded)) parent.cancel();
                    }));
            equal(entered.get(), phase.equals("start") ? 0 : 1);
            equal(parent.isCancelled(), !phase.equals("handler"));
            equal(events.stream().filter(AgentEvent.ToolStarted.class::isInstance).count(), 1L);
            equal(events.stream().filter(AgentEvent.ToolEnded.class::isInstance).count(), 1L);
            truth(events.getLast() instanceof AgentEvent.Aborted, "Missing abort");
        }
    }

    private static void heldCleanup() throws Exception {
        for (boolean interrupt : List.of(false, true)) {
            var entered = new CountDownLatch(1);
            var wake = new CountDownLatch(1);
            var cleaning = new CountDownLatch(1);
            var release = new CountDownLatch(1);
            var closed = new AtomicBoolean();
            var failure = new AtomicReference<Throwable>();
            var events = new CopyOnWriteArrayList<AgentEvent>();
            var parent = new CancellationToken();
            var tool = echo((arguments, token) -> {
                var registration = token.onCancel(wake::countDown);
                try { entered.countDown(); await(wake); token.check(); return "unreachable"; }
                finally {
                    registration.close();
                    cleaning.countDown();
                    await(release);
                    closed.set(true);
                }
            });
            var runtime = new Agent(rounds(List.of(call("1", Json.MAPPER.createObjectNode().put("text", "wait"))), () -> {}),
                    "fixture", "", List.of(tool));
            var interrupted = new AtomicBoolean();
            var running = worker(() -> {
                try {
                    runtime.run("go", List.of(), parent, event -> {
                        if (event instanceof AgentEvent.ToolEnded) truth(closed.get(), "End preceded handler cleanup");
                        events.add(event);
                    });
                } finally { interrupted.set(Thread.currentThread().isInterrupted()); }
            }, failure);
            try {
                await(entered);
                if (interrupt) running.interrupt(); else parent.cancel();
                await(cleaning);
                truth(running.isAlive(), "Run escaped held cleanup");
                equal(events.stream().filter(AgentEvent.ToolEnded.class::isInstance).count(), 0L);
            } finally { release.countDown(); join(running); }
            truth(closed.get(), "Handler was not closed");
            equal(events.stream().filter(AgentEvent.ToolEnded.class::isInstance).count(), 1L);
            truth(interrupt ? failure.get() instanceof InterruptedException : failure.get() instanceof CancellationException,
                    "Lost cancellation or interruption: " + failure.get());
            equal(interrupted.get(), interrupt);
        }
    }

    private static void callbackOwnership() throws Exception {
        for (boolean start : List.of(false, true)) {
            var failure = new IOException("consumer failed");
            var events = new ArrayList<AgentEvent>();
            var tool = echo((arguments, token) -> "ok");
            var runtime = new Agent(rounds(List.of(call("1", Json.MAPPER.createObjectNode().put("text", "ok"))), () -> {}),
                    "fixture", "", List.of(tool));
            equal(expect(IOException.class, () -> runtime.run("go", List.of(), new CancellationToken(), event -> {
                events.add(event);
                if (start ? event instanceof AgentEvent.ToolStarted : event instanceof AgentEvent.ToolEnded) throw failure;
            })), failure);
            truth(start ? events.getLast() instanceof AgentEvent.ToolStarted : events.getLast() instanceof AgentEvent.ToolEnded,
                    "Delivered after consumer failure");
        }
        var retained = new AtomicReference<Provider.TextSink>();
        Provider provider = (request, token, text) -> {
            if (retained.compareAndSet(null, text)) {
                return new ProviderTurn("", List.of(call("1", Json.MAPPER.createObjectNode().put("text", "ok"))), List.of(), List.of());
            }
            expect(CancellationException.class, () -> retained.get().accept("late"));
            return CoreChecks.turn("done");
        };
        var tool = echo((arguments, token) -> {
            expect(CancellationException.class, () -> retained.get().accept("late"));
            return "ok";
        });
        equal(new Agent(provider, "fixture", "", List.of(tool)).run("go").text(), "done");
        expect(CancellationException.class, () -> retained.get().accept("late"));
    }

    private static void session() throws Exception {
        var events = new CopyOnWriteArrayList<ObjectNode>();
        var failTerminal = new AtomicBoolean();
        var tool = echo((arguments, token) -> "ok");
        var runtime = new Agent(rounds(List.of(call("1", Json.MAPPER.createObjectNode().put("text", "ok"))), () -> {}),
                "fixture", "", List.of(tool));
        try (var session = new RpcSession(runtime, (event, token) -> {
            if (failTerminal.get() && event.path("type").stringValue().equals("agent_end")) throw new IOException("disconnect");
            events.add(event);
        })) {
            session.handle(Json.object("prompt").put("id", "go").put("message", "go"));
            session.awaitIdle();
            equal(events.getFirst().path("type").stringValue(), "response");
            equal(events.getLast().path("status").stringValue(), "completed");
            equal(session.state().path("messageCount").intValue(), 4);
            failTerminal.set(true);
            session.handle(Json.object("prompt").put("message", "again"));
            session.awaitIdle();
            equal(session.state().path("messageCount").intValue(), 4);
        }
    }
}
