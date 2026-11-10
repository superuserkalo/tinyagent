package dev.tinyagent;

/**
 * Emits text in order, then returns exactly one complete turn.
 * The caller owns its execution thread and cancellation token.
 */
public interface Provider extends AutoCloseable {
    @FunctionalInterface
    interface TextSink {
        void accept(String delta) throws Exception;
    }

    ProviderTurn stream(ProviderRequest request, CancellationToken cancellation, TextSink text) throws Exception;

    @Override
    default void close() {}
}
