package dev.tinyagent;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.concurrent.CancellationException;

/** Run-scoped cancellation. Callbacks should release resources promptly. */
public final class CancellationToken {
    @FunctionalInterface
    public interface Registration extends AutoCloseable {
        @Override
        void close();
    }

    private final LinkedHashMap<Object, Runnable> callbacks = new LinkedHashMap<>();
    private boolean cancelled;

    public synchronized boolean isCancelled() {
        return cancelled;
    }

    public void check() {
        if (isCancelled() || Thread.currentThread().isInterrupted()) {
            throw new CancellationException("Run cancelled");
        }
    }

    public Registration onCancel(Runnable callback) {
        var key = new Object();
        synchronized (this) {
            if (!cancelled) {
                callbacks.put(key, callback);
                return () -> {
                    synchronized (CancellationToken.this) {
                        callbacks.remove(key);
                    }
                };
            }
        }
        callback.run();
        return () -> {};
    }

    public void cancel() {
        ArrayList<Runnable> pending;
        synchronized (this) {
            if (cancelled) return;
            cancelled = true;
            pending = new ArrayList<>(callbacks.values());
            callbacks.clear();
        }
        RuntimeException failure = null;
        for (var callback : pending) {
            try {
                callback.run();
            } catch (RuntimeException error) {
                if (failure == null) failure = error;
                else if (failure != error) failure.addSuppressed(error);
            }
        }
        if (failure != null) throw failure;
    }
}
