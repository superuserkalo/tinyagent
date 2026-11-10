package dev.tinyagent;

import static dev.tinyagent.ProviderChecks.await;
import static dev.tinyagent.ProviderChecks.check;
import static dev.tinyagent.ProviderChecks.equal;
import static dev.tinyagent.ProviderChecks.expect;
import static dev.tinyagent.ProviderChecks.join;
import static dev.tinyagent.ProviderChecks.truth;
import static dev.tinyagent.ProviderChecks.worker;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CancellationException;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import tools.jackson.databind.node.ObjectNode;

final class CoreChecks {
    private CoreChecks() {}

    static void run() {
        check("Core native provider runs and ordered events", CoreChecks::nativeRuns);
        check("Core completed history and replay snapshots", CoreChecks::history);
        check("Core typed protocol event values", CoreChecks::eventValues);
        check("Core provider failure and unknown tools", CoreChecks::providerFailure);
        check("Core pre-cancellation and start cancellation", CoreChecks::preCancellation);
        check("Core cancellation cannot become success", CoreChecks::cancellationWins);
        check("Core native cancellation releases headers and body", CoreChecks::nativeCancellation);
        check("Core interruption and resource ownership", CoreChecks::interruption);
        check("Core consumer failure cannot become success", CoreChecks::consumerFailure);
        check("Core terminal delivery can prevent a result", CoreChecks::terminalFailure);
        check("Core cleanup finishes before terminal delivery", CoreChecks::cleanup);
        check("Core held callbacks retain run ownership", CoreChecks::heldCallback);
        check("Core concurrent runs are independent", CoreChecks::concurrentRuns);
        check("Core configuration and Unicode validation", CoreChecks::invalidValues);
    }

    static ProviderTurn turn(String text) {
        return new ProviderTurn(text, List.of(), List.of(Json.object("opaque").put("signature", "keep")),
                List.of(Json.object("result").put("value", 1)));
    }

    private static Agent agent(Provider provider) {
        return new Agent(provider, "fixture-model", "Fixture instructions");
    }

    private static void ordered(List<AgentEvent> events, String status) {
        truth(events.getFirst() instanceof AgentEvent.Start, "Missing start event");
        equal(events.getLast().toJson().path("status").stringValue(), status);
        long terminals = events.stream().filter(event -> event.toJson().path("type").stringValue().equals("agent_end")).count();
        equal(terminals, 1L);
        for (int index = 0; index < events.size(); index++) {
            equal(events.get(index).sequence(), (long) index + 1);
            equal(events.get(index).runId(), events.getFirst().runId());
        }
    }

    private static void nativeRuns() throws Exception {
        for (boolean anthropic : List.of(false, true)) {
            var client = new FixtureClient(ProviderChecks.fixture(anthropic, false), true, false, 200);
            var provider = ProviderChecks.provider(anthropic, client, ProviderChecks.TIMEOUT);
            var events = new ArrayList<AgentEvent>();
            var result = agent(provider).run("Hello", List.of(), new CancellationToken(), "r_fixture", events::add);
            ordered(events, "completed");
            equal(result.text(), "done\ncafé");
            equal(result.history().size(), 2);
            equal(events.stream().filter(AgentEvent.TextDelta.class::isInstance)
                    .map(AgentEvent.TextDelta.class::cast).map(AgentEvent.TextDelta::delta).toList(), List.of("done\n", "café"));
            equal(events.getFirst().runId(), "r_fixture");
            truth(client.cancelled.get(), "Native response was not closed");
            truth(!client.shutDown.get(), "Agent closed its borrowed provider client");
            var request = Json.MAPPER.readTree(client.requestBytes());
            truth(request.path("tools").isMissingNode() || request.path("tools").isEmpty(), "Core registered an executable tool");
        }
    }

    private static void history() throws Exception {
        var requests = new ArrayList<ProviderRequest>();
        var runtime = agent((request, token, text) -> {
            requests.add(request);
            return turn("answer " + requests.size());
        });
        var first = runtime.run("first");
        var second = runtime.run("second", first.history(), new CancellationToken(), event -> {});
        equal(first.history().size(), 2);
        equal(second.history().size(), 4);
        equal(second.text(), "answer 2");
        var replay = (Message.Assistant) requests.getLast().history().get(1);
        equal(replay.replay().getFirst().path("signature").stringValue(), "keep");
        ((ObjectNode) replay.replay().getFirst()).put("signature", "mutated");
        equal(((Message.Assistant) second.history().get(1)).replay().getFirst().path("signature").stringValue(), "keep");
        ((ObjectNode) second.output().getFirst()).put("value", 2);
        equal(second.output().getFirst().path("value").intValue(), 1);
        expect(UnsupportedOperationException.class, () -> first.history().clear());
        expect(UnsupportedOperationException.class, () -> requests.getFirst().history().clear());
    }

    private static void eventValues() throws Exception {
        var text = new AgentEvent.TextDelta("r", 2, "café\n");
        var json = text.toJson();
        json.put("delta", "changed");
        equal(text.toJson().path("delta").stringValue(), "café\n");
        equal(new AgentEvent.Start("r", 1).toJson().path("type").stringValue(), "agent_start");
        equal(new AgentEvent.Failed("r", 3, "failure").toJson().path("error").path("code").stringValue(), "provider_error");
        truth(!new AgentEvent.Aborted("r", 3).toJson().has("text"), "Aborted event has completion text");
        expect(IllegalArgumentException.class, () -> new AgentEvent.Start("", 1));
        expect(IllegalArgumentException.class, () -> new AgentEvent.Start("r", 0));
    }

    private static void providerFailure() throws Exception {
        var previous = new ArrayList<Message>(List.of(new Message.User("old")));
        for (boolean partial : List.of(false, true)) {
            var failure = new ProviderException("broken provider");
            var events = new ArrayList<AgentEvent>();
            var runtime = agent((request, token, text) -> {
                if (partial) text.accept("partial");
                throw failure;
            });
            equal(expect(ProviderException.class, () -> runtime.run("new", previous, new CancellationToken(), events::add)), failure);
            ordered(events, "failed");
            equal(previous, List.of(new Message.User("old")));
        }
        var call = new ToolCall("c", "echo", Json.MAPPER.createObjectNode());
        var events = new ArrayList<AgentEvent>();
        var runtime = agent((request, token, text) -> new ProviderTurn("", List.of(call), List.of(), List.of()));
        expect(ProviderException.class, () -> runtime.run("go", previous, new CancellationToken(), events::add));
        ordered(events, "failed");
        equal(events.size(), 2);
    }

    private static void preCancellation() throws Exception {
        for (boolean before : List.of(false, true)) {
            var parent = new CancellationToken();
            if (before) parent.cancel();
            var events = new ArrayList<AgentEvent>();
            var runtime = agent((request, token, text) -> { throw new AssertionError("Provider ran after cancellation"); });
            expect(CancellationException.class, () -> runtime.run("go", List.of(), parent, event -> {
                events.add(event);
                if (event instanceof AgentEvent.Start) parent.cancel();
            }));
            ordered(events, "aborted");
        }
    }

    private static void cancellationWins() throws Exception {
        for (boolean throwsError : List.of(false, true)) {
            var parent = new CancellationToken();
            var events = new ArrayList<AgentEvent>();
            var runtime = agent((request, token, text) -> {
                parent.cancel();
                if (throwsError) throw new IOException("incidental error");
                return turn("must not return");
            });
            expect(CancellationException.class, () -> runtime.run("go", List.of(), parent, events::add));
            ordered(events, "aborted");
        }
    }

    private static void nativeCancellation() throws Exception {
        for (boolean anthropic : List.of(false, true)) {
            for (boolean headers : List.of(false, true)) {
                var client = new FixtureClient(new byte[0], true, headers, 200);
                var runtime = agent(ProviderChecks.provider(anthropic, client, ProviderChecks.TIMEOUT));
                var parent = new CancellationToken();
                var events = new ArrayList<AgentEvent>();
                var failure = new AtomicReference<Throwable>();
                var task = worker(() -> runtime.run("go", List.of(), parent, events::add), failure);
                await(headers ? client.started : client.subscribed);
                parent.cancel();
                join(task);
                truth(failure.get() instanceof CancellationException, "Cancellation did not propagate");
                truth(client.cancelled.get(), "Cancelled request stayed open");
                ordered(events, "aborted");
            }
        }
    }

    private static void interruption() throws Exception {
        var client = new FixtureClient(new byte[0], true, true, 200);
        var runtime = agent(ProviderChecks.provider(false, client, ProviderChecks.TIMEOUT));
        var failure = new AtomicReference<Throwable>();
        var interrupted = new AtomicBoolean();
        var events = new ArrayList<AgentEvent>();
        var task = worker(() -> {
            try { runtime.run("go", List.of(), new CancellationToken(), events::add); }
            finally { interrupted.set(Thread.currentThread().isInterrupted()); }
        }, failure);
        await(client.started);
        task.interrupt();
        join(task);
        truth(failure.get() instanceof InterruptedException, "Native interrupt exception was lost");
        truth(interrupted.get() && client.cancelled.get(), "Interrupt state or cleanup was lost");
        ordered(events, "aborted");
    }

    private static void consumerFailure() throws Exception {
        var failure = new IOException("consumer failed");
        var token = new AtomicReference<CancellationToken>();
        var events = new ArrayList<AgentEvent>();
        var runtime = agent((request, cancellation, text) -> {
            token.set(cancellation);
            try { text.accept("partial"); }
            catch (IOException ignored) { /* Misbehaving providers cannot turn this into success. */ }
            return turn("wrong result");
        });
        equal(expect(IOException.class, () -> runtime.run("go", List.of(), new CancellationToken(), event -> {
            events.add(event);
            if (event instanceof AgentEvent.TextDelta) throw failure;
        })), failure);
        equal(events.size(), 2);
        truth(token.get().isCancelled(), "Consumer failure did not close run token");
        for (boolean anthropic : List.of(false, true)) {
            var client = new FixtureClient(ProviderChecks.fixture(anthropic, false), true, false, 200);
            var nativeRuntime = agent(ProviderChecks.provider(anthropic, client, ProviderChecks.TIMEOUT));
            equal(expect(IOException.class, () -> nativeRuntime.run("go", List.of(), new CancellationToken(), event -> {
                if (event instanceof AgentEvent.TextDelta) throw failure;
            })), failure);
            truth(client.cancelled.get(), "Failed consumer retained native body");
        }
    }

    private static void terminalFailure() throws Exception {
        for (boolean cancel : List.of(false, true)) {
            var parent = new CancellationToken();
            var events = new ArrayList<AgentEvent>();
            var failure = new IOException("terminal write failed");
            var runtime = agent((request, token, text) -> turn("done"));
            Agent.EventSink sink = event -> {
                events.add(event);
                if (event instanceof AgentEvent.Completed) {
                    if (cancel) parent.cancel();
                    else throw failure;
                }
            };
            if (cancel) expect(CancellationException.class, () -> runtime.run("go", List.of(), parent, sink));
            else equal(expect(IOException.class, () -> runtime.run("go", List.of(), parent, sink)), failure);
            ordered(events, "completed");
        }
    }

    private static void cleanup() throws Exception {
        for (boolean fails : List.of(false, true)) {
            var closed = new AtomicBoolean();
            var failure = new IllegalStateException("cleanup failed");
            var events = new ArrayList<AgentEvent>();
            var runtime = agent((request, token, text) -> {
                token.onCancel(() -> { closed.set(true); if (fails) throw failure; });
                return turn("done");
            });
            Agent.EventSink sink = event -> {
                if (!(event instanceof AgentEvent.Start)) truth(closed.get(), "Terminal preceded cleanup");
                events.add(event);
            };
            if (fails) equal(expect(IllegalStateException.class, () -> runtime.run("go", List.of(), new CancellationToken(), sink)), failure);
            else equal(runtime.run("go", List.of(), new CancellationToken(), sink).text(), "done");
            ordered(events, fails ? "failed" : "completed");
        }
    }

    private static void heldCallback() throws Exception {
        var entered = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        var parent = new CancellationToken();
        var failure = new AtomicReference<Throwable>();
        var events = new ArrayList<AgentEvent>();
        var runtime = agent((request, token, text) -> { text.accept("partial"); return turn("done"); });
        var task = worker(() -> runtime.run("go", List.of(), parent, event -> {
            events.add(event);
            if (event instanceof AgentEvent.TextDelta) { entered.countDown(); await(release); }
        }), failure);
        try {
            await(entered);
            parent.cancel();
            truth(task.isAlive(), "Run returned before its callback finished");
        } finally {
            release.countDown();
            join(task);
        }
        truth(failure.get() instanceof CancellationException, "Held callback cancellation did not propagate");
        ordered(events, "aborted");
    }

    private static void concurrentRuns() throws Exception {
        var entered = new CountDownLatch(2);
        var release = new CountDownLatch(1);
        var tokens = java.util.concurrent.ConcurrentHashMap.<CancellationToken>newKeySet();
        var runtime = agent((request, token, text) -> {
            tokens.add(token);
            entered.countDown();
            await(release);
            token.check();
            return turn(((Message.User) request.history().getLast()).text());
        });
        var firstParent = new CancellationToken();
        var firstEvents = new ArrayList<AgentEvent>();
        var secondEvents = new ArrayList<AgentEvent>();
        var firstFailure = new AtomicReference<Throwable>();
        var secondFailure = new AtomicReference<Throwable>();
        var answer = new AtomicReference<RunResult>();
        var first = worker(() -> runtime.run("first", List.of(), firstParent, firstEvents::add), firstFailure);
        var second = worker(() -> answer.set(runtime.run("second", List.of(), new CancellationToken(), secondEvents::add)), secondFailure);
        try {
            await(entered);
            equal(tokens.size(), 2);
            firstParent.cancel();
        } finally {
            release.countDown();
            join(first);
            join(second);
        }
        truth(firstFailure.get() instanceof CancellationException, "First run did not cancel");
        truth(secondFailure.get() == null, "Second run was cancelled with first");
        equal(answer.get().text(), "second");
        ordered(firstEvents, "aborted");
        ordered(secondEvents, "completed");
        truth(!firstEvents.getFirst().runId().equals(secondEvents.getFirst().runId()), "Runs reused an ID");
    }

    private static void invalidValues() throws Exception {
        var calls = new AtomicInteger();
        var runtime = agent((request, token, text) -> { calls.incrementAndGet(); return turn("done"); });
        expect(IllegalArgumentException.class, () -> new Agent((request, token, text) -> turn(""), "", ""));
        expect(IllegalArgumentException.class, () -> runtime.run("\ud800"));
        expect(IllegalArgumentException.class, () -> runtime.run("go", List.of(), new CancellationToken(), "", event -> {}));
        equal(calls.get(), 0);
        for (String invalid : List.of("\ud800", "\udc00")) {
            var events = new ArrayList<AgentEvent>();
            var broken = agent((request, token, text) -> { text.accept(invalid); return turn("done"); });
            expect(IllegalArgumentException.class, () -> broken.run("go", List.of(), new CancellationToken(), events::add));
            ordered(events, "failed");
        }
        var events = new ArrayList<AgentEvent>();
        expect(ProviderException.class, () -> agent((request, token, text) -> null).run("go", List.of(), new CancellationToken(), events::add));
        ordered(events, "failed");
    }
}
