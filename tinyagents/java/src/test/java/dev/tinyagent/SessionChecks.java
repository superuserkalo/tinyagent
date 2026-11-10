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
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

final class SessionChecks {
    private static final Session.BeforeRun READY = (id, token) -> {};
    private static final Session.Events DISCARD = (event, token) -> {};

    private SessionChecks() {}

    static void run() {
        check("Session admission and terminal commit boundary", SessionChecks::admission);
        check("Session history survives failed runs", SessionChecks::rollback);
        check("Session reset cancels and joins old work", SessionChecks::reset);
        check("Session stop and close retain worker ownership", SessionChecks::close);
        check("Session acknowledgement failure prevents provider work", SessionChecks::acknowledgement);
        check("Session cancellation callbacks run outside its lock", SessionChecks::cancellationCallbacks);
        check("Session cleanup failure prevents history commit", SessionChecks::cleanupFailure);
        check("Session callback shutdown avoids self-join", SessionChecks::callbackShutdown);
        check("Session independent workers and histories", SessionChecks::independent);
        check("Session inputs are rejected before admission", SessionChecks::invalidInputs);
    }

    private static Session session(Provider provider) {
        return new Session(new Agent(provider, "fixture-model", "Fixture instructions"));
    }

    private static void complete(Session session, String prompt) {
        session.start(prompt, READY, DISCARD);
        truth(session.awaitIdle(), "Caller did not join session worker");
    }

    private static void admission() throws Exception {
        var entered = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        var order = new ArrayList<String>();
        var requests = new ArrayList<ProviderRequest>();
        try (var session = session((request, token, text) -> {
            requests.add(request);
            return CoreChecks.turn("done");
        })) {
            var id = session.start("first", (runId, token) -> order.add("ack:" + runId), (event, token) -> {
                order.add(event.toJson().path("type").stringValue());
                if (event instanceof AgentEvent.Completed) { entered.countDown(); await(release); }
            });
            try {
                await(entered);
                equal(session.state(), new Session.State(id, 0));
                expect(Session.BusyException.class, () -> session.start("busy", READY, DISCARD));
                equal(order, List.of("ack:" + id, "agent_start", "text_delta", "agent_end"));
            } finally { release.countDown(); session.awaitIdle(); }
            equal(session.state(), new Session.State(null, 2));
            complete(session, "second");
            equal(session.state().messageCount(), 4);
            equal(requests.getLast().history().size(), 3);
            equal(((Message.User) requests.getLast().history().getFirst()).text(), "first");
        }
    }

    private static void rollback() {
        for (String mode : List.of("provider", "consumer", "cancel")) {
            var requests = new ArrayList<ProviderRequest>();
            try (var session = session((request, token, text) -> {
                requests.add(request);
                if (requests.size() == 2 && mode.equals("provider")) throw new ProviderException("failed");
                return CoreChecks.turn("done");
            })) {
                complete(session, "seed");
                session.start("failed turn", READY, (event, token) -> {
                    if (event instanceof AgentEvent.Completed) {
                        if (mode.equals("consumer")) throw new IOException("delivery failed");
                        if (mode.equals("cancel")) token.cancel();
                    }
                });
                session.awaitIdle();
                equal(session.state(), new Session.State(null, 2));
                complete(session, "next");
                equal(requests.getLast().history().size(), 3);
                truth(requests.getLast().history().stream().noneMatch(message ->
                        message instanceof Message.User user && user.text().equals("failed turn")), "Failed history was committed");
            }
        }
    }

    private static void reset() throws Exception {
        var entered = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        var cancelled = new CountDownLatch(1);
        var failure = new AtomicReference<Throwable>();
        var resetDone = new AtomicBoolean();
        try (var session = session((request, token, text) -> CoreChecks.turn("done"))) {
            complete(session, "seed");
            session.start("held", (id, token) -> token.onCancel(cancelled::countDown), (event, token) -> {
                if (event instanceof AgentEvent.Completed) { entered.countDown(); await(release); }
            });
            await(entered);
            var resetter = worker(() -> { truth(session.reset(), "Reset missed active run"); resetDone.set(true); }, failure);
            try {
                await(cancelled);
                truth(session.state().running(), "Reset released active ownership before cleanup");
                equal(session.state().messageCount(), 0);
                truth(!resetDone.get(), "Reset returned before old callback finished");
                expect(Session.BusyException.class, () -> session.start("busy", READY, DISCARD));
                expect(Session.BusyException.class, session::reset);
            } finally { release.countDown(); join(resetter); }
            truth(failure.get() == null && resetDone.get(), "Reset did not complete");
            equal(session.state(), new Session.State(null, 0));
            complete(session, "fresh");
            equal(session.state().messageCount(), 2);
            truth(!session.reset(), "Idle reset reported an active run");
            equal(session.state().messageCount(), 0);
        }
    }

    private static void close() throws Exception {
        var entered = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        var closing = new CountDownLatch(1);
        var finished = new AtomicBoolean();
        var interrupted = new AtomicBoolean();
        var failure = new AtomicReference<Throwable>();
        var session = session((request, token, text) -> CoreChecks.turn("done"));
        var id = session.start("held", READY, (event, token) -> {
            if (event instanceof AgentEvent.Completed) { entered.countDown(); await(release); }
        });
        await(entered);
        equal(session.stop(), id);
        expect(Session.ClosedException.class, () -> session.start("closed", READY, DISCARD));
        expect(Session.ClosedException.class, session::reset);
        var closer = worker(() -> {
            Thread.currentThread().interrupt();
            closing.countDown();
            session.close();
            interrupted.set(Thread.currentThread().isInterrupted());
            finished.set(true);
        }, failure);
        try {
            await(closing);
            truth(!finished.get() && session.state().running(), "Close dropped its live worker");
        } finally { release.countDown(); join(closer); session.close(); }
        truth(failure.get() == null && finished.get() && interrupted.get(), "Close lost cleanup or caller interruption");
        equal(session.state(), new Session.State(null, 0));
        truth(session.cancel() == null, "Idle session reported a cancelled run");
    }

    private static void acknowledgement() {
        var called = new AtomicBoolean();
        var events = new ArrayList<AgentEvent>();
        try (var session = session((request, token, text) -> { called.set(true); return CoreChecks.turn("done"); })) {
            session.start("go", (id, token) -> { throw new IOException("ack failed"); }, (event, token) -> events.add(event));
            session.awaitIdle();
            truth(!called.get() && events.isEmpty(), "Failed acknowledgement started a run");
            equal(session.state(), new Session.State(null, 0));
            complete(session, "next");
            equal(session.state().messageCount(), 2);
        }
    }

    private static void cancellationCallbacks() throws Exception {
        var entered = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        var failure = new AtomicReference<Throwable>();
        var callbackRan = new AtomicBoolean();
        try (var session = session((request, token, text) -> {
            entered.countDown(); await(release); token.check(); return CoreChecks.turn("done");
        })) {
            session.start("go", (id, token) -> token.onCancel(() -> {
                var reader = worker(() -> { truth(session.state().running(), "Missing active session"); callbackRan.set(true); }, failure);
                try { join(reader); }
                catch (Exception error) { throw new IllegalStateException(error); }
            }), DISCARD);
            try { await(entered); session.cancel(); }
            finally { release.countDown(); session.awaitIdle(); }
            truth(failure.get() == null && callbackRan.get(), "Cancellation held the session lock");
            equal(session.state().messageCount(), 0);
        }
    }

    private static void cleanupFailure() {
        try (var session = session((request, token, text) -> CoreChecks.turn("done"))) {
            session.start("go", (id, token) -> token.onCancel(() -> { throw new IllegalStateException("release failed"); }), DISCARD);
            session.awaitIdle();
            equal(session.state(), new Session.State(null, 0));
            complete(session, "next");
            equal(session.state().messageCount(), 2);
        }
    }

    private static void callbackShutdown() {
        for (boolean reset : List.of(false, true)) {
            var session = session((request, token, text) -> CoreChecks.turn("done"));
            try {
                var called = new AtomicBoolean();
                session.start("go", READY, (event, token) -> {
                    if (event instanceof AgentEvent.Completed) {
                        truth(!session.awaitIdle(), "Worker tried to join itself");
                        if (reset) session.reset();
                        else session.close();
                        called.set(true);
                    }
                });
                session.awaitIdle();
                truth(called.get(), "Callback shutdown failed");
                equal(session.state(), new Session.State(null, 0));
                if (reset) { complete(session, "next"); equal(session.state().messageCount(), 2); }
            } finally { session.close(); }
        }
    }

    private static void independent() throws Exception {
        var entered = new CountDownLatch(2);
        var release = new CountDownLatch(1);
        Provider provider = (request, token, text) -> {
            entered.countDown(); await(release); token.check(); return CoreChecks.turn("done");
        };
        try (var first = session(provider); var second = session(provider)) {
            first.start("first", READY, DISCARD);
            second.start("second", READY, DISCARD);
            try { await(entered); first.cancel(); }
            finally { release.countDown(); first.awaitIdle(); second.awaitIdle(); }
            equal(first.state(), new Session.State(null, 0));
            equal(second.state(), new Session.State(null, 2));
        }
    }

    private static void invalidInputs() throws Exception {
        try (var session = session((request, token, text) -> { throw new AssertionError("Invalid run reached provider"); })) {
            expect(IllegalArgumentException.class, () -> session.start("\ud800", READY, DISCARD));
            expect(NullPointerException.class, () -> session.start("go", null, DISCARD));
            expect(NullPointerException.class, () -> session.start("go", READY, null));
            equal(session.state(), new Session.State(null, 0));
        }
    }
}
