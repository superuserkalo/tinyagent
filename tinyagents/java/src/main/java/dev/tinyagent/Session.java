package dev.tinyagent;

import java.util.List;
import java.util.Objects;
import java.util.UUID;

/** Transport-owned history and one worker, retained through delivery and cleanup. */
final class Session implements AutoCloseable {
    @FunctionalInterface
    interface BeforeRun {
        void accept(String runId, CancellationToken cancellation) throws Exception;
    }

    @FunctionalInterface
    interface Events {
        void accept(AgentEvent event, CancellationToken cancellation) throws Exception;
    }

    record State(String runId, int messageCount) {
        boolean running() { return runId != null; }
    }

    static final class BusyException extends IllegalStateException {
        private static final long serialVersionUID = 1L;
        BusyException() { super("A run or reset is already active"); }
    }

    static final class ClosedException extends IllegalStateException {
        private static final long serialVersionUID = 1L;
        ClosedException() { super("Session is closed"); }
    }

    private static final class ActiveRun {
        final String id = "r_" + UUID.randomUUID();
        final CancellationToken token = new CancellationToken();
        boolean cancelRequested;
    }

    private final Agent agent;
    private final Object lock = new Object();
    private List<Message> history = List.of();
    private ActiveRun active;
    private Thread worker;
    private boolean resetting;
    private boolean closed;

    Session(Agent agent) { this.agent = Objects.requireNonNull(agent); }

    String start(String prompt, BeforeRun beforeRun, Events events) {
        Json.text(prompt, "prompt");
        Objects.requireNonNull(beforeRun);
        Objects.requireNonNull(events);
        synchronized (lock) {
            if (closed) throw new ClosedException();
            if (active != null || resetting) throw new BusyException();
            var run = new ActiveRun();
            var previous = history;
            var thread = Thread.ofVirtual().unstarted(() -> execute(run, prompt, previous, beforeRun, events));
            active = run;
            try {
                thread.start();
                worker = thread;
            } catch (RuntimeException | Error failure) {
                active = null;
                throw failure;
            }
            return run.id;
        }
    }

    State state() {
        synchronized (lock) {
            return new State(active == null ? null : active.id, history.size());
        }
    }

    String cancel() {
        ActiveRun run;
        synchronized (lock) { run = requestCancellation(); }
        return cancel(run);
    }

    boolean reset() {
        ActiveRun run;
        synchronized (lock) {
            if (closed) throw new ClosedException();
            if (resetting) throw new BusyException();
            resetting = true;
            history = List.of();
            run = requestCancellation();
        }
        try {
            cancel(run);
            return run != null;
        } finally {
            awaitIdle();
            synchronized (lock) { resetting = false; }
        }
    }

    // I/O adapters can stop admission and keep draining before joining the worker.
    String stop() {
        ActiveRun run;
        synchronized (lock) {
            closed = true;
            run = requestCancellation();
        }
        return cancel(run);
    }

    @Override
    public void close() {
        try { stop(); }
        finally { awaitIdle(); }
    }

    // Join the observed worker, including its final cleanup. A callback cannot join itself.
    boolean awaitIdle() {
        Thread thread;
        synchronized (lock) { thread = worker; }
        if (thread == Thread.currentThread()) return false;
        boolean interrupted = false;
        try {
            while (thread != null && thread.isAlive()) {
                try { thread.join(); }
                catch (InterruptedException ignored) { interrupted = true; }
            }
            return true;
        } finally {
            if (interrupted) Thread.currentThread().interrupt();
        }
    }

    private ActiveRun requestCancellation() {
        if (active != null) active.cancelRequested = true;
        return active;
    }

    private static String cancel(ActiveRun run) {
        if (run == null) return null;
        run.token.cancel();
        return run.id;
    }

    private void execute(ActiveRun run, String prompt, List<Message> previous, BeforeRun beforeRun, Events events) {
        RunResult result = null;
        try {
            beforeRun.accept(run.id, run.token);
            result = agent.run(prompt, previous, run.token, run.id, event -> events.accept(event, run.token));
        } catch (Exception failure) {
            // The core reports provider failures. Failed delivery has no usable peer.
            if (failure instanceof InterruptedException) Thread.currentThread().interrupt();
        } finally {
            boolean cancelled = run.token.isCancelled();
            boolean cleaned = false;
            try {
                run.token.cancel();
                cleaned = true;
            } catch (RuntimeException ignored) {
                // A failed release prevents committing this result.
            } finally {
                synchronized (lock) {
                    if (cleaned && result != null && !cancelled && !run.cancelRequested && !closed && !resetting) {
                        history = result.history();
                    }
                    active = null;
                }
            }
        }
    }
}
