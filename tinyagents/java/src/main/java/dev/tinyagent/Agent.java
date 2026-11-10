package dev.tinyagent;

import java.util.ArrayList;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.UUID;
import java.util.concurrent.CancellationException;
import java.util.concurrent.atomic.AtomicBoolean;

/** Immutable configuration. The caller owns the provider and its lifetime. */
public final class Agent {
    @FunctionalInterface
    public interface EventSink {
        void accept(AgentEvent event) throws Exception;
    }

    private final Provider provider;
    private final String model;
    private final String instructions;
    private final Map<String, Tool> tools;
    private final List<ToolDefinition> definitions;

    public Agent(Provider provider, String model, String instructions) {
        this(provider, model, instructions, List.of());
    }

    public Agent(Provider provider, String model, String instructions, List<Tool> tools) {
        this.provider = Objects.requireNonNull(provider);
        this.model = Json.text(model, "model");
        this.instructions = Json.text(instructions, "instructions");
        if (model.isEmpty()) throw new IllegalArgumentException("model must be nonempty");
        var registered = new LinkedHashMap<String, Tool>();
        for (var tool : List.copyOf(tools)) {
            if (registered.putIfAbsent(tool.definition().name(), tool) != null) {
                throw new IllegalArgumentException("Tool names must be unique");
            }
        }
        this.tools = Map.copyOf(registered);
        definitions = registered.values().stream().map(Tool::definition).toList();
    }

    public RunResult run(String prompt) throws Exception {
        return run(prompt, List.of(), new CancellationToken(), event -> {});
    }

    public RunResult run(String prompt, List<Message> history, CancellationToken cancellation, EventSink events)
            throws Exception {
        return run(prompt, history, cancellation, "r_" + UUID.randomUUID(), events);
    }

    public RunResult run(String prompt, List<Message> history, CancellationToken cancellation, String runId, EventSink events)
            throws Exception {
        Json.text(prompt, "prompt");
        Objects.requireNonNull(cancellation);
        Objects.requireNonNull(events);
        Json.text(runId, "runId");
        if (runId.isEmpty()) throw new IllegalArgumentException("runId must be nonempty");
        var transcript = new ArrayList<>(List.copyOf(history));
        transcript.add(new Message.User(prompt));
        return new Run(transcript, cancellation, runId, events).execute();
    }

    private final class Run {
        private final ArrayList<Message> history;
        private final CancellationToken parent;
        private final CancellationToken token = new CancellationToken();
        private final String id;
        private final EventSink events;
        private long sequence;
        private Throwable consumerFailure;

        Run(ArrayList<Message> history, CancellationToken parent, String id, EventSink events) {
            this.history = history;
            this.parent = parent;
            this.id = id;
            this.events = events;
        }

        RunResult execute() throws Exception {
            var registration = parent.onCancel(token::cancel);
            Throwable failure = null;
            boolean cancelled;
            RunResult result = null;
            try {
                emit(new AgentEvent.Start(id, ++sequence));
                token.check();
                while (true) {
                    var streamed = new AtomicBoolean();
                    var requestToken = new CancellationToken();
                    var requestLink = token.onCancel(requestToken::cancel);
                    ProviderTurn turn;
                    try {
                        turn = provider.stream(new ProviderRequest(model, instructions, history, definitions), requestToken, delta -> {
                            if (consumerFailure != null) rethrow(consumerFailure);
                            requestToken.check();
                            Json.text(delta, "provider text delta");
                            if (!delta.isEmpty()) {
                                streamed.set(true);
                                emit(new AgentEvent.TextDelta(id, ++sequence, delta));
                            }
                            requestToken.check();
                        });
                        requestToken.check();
                    } finally {
                        requestLink.close();
                        if (requestToken.isCancelled()) token.cancel();
                        requestToken.cancel();
                    }
                    if (consumerFailure != null) rethrow(consumerFailure);
                    token.check();
                    if (turn == null) throw new ProviderException("Provider did not return a completed turn");
                    Json.text(turn.text(), "provider final text");
                    for (var call : turn.toolCalls()) {
                        if (!tools.containsKey(call.name())) {
                            throw new ProviderException("Provider requested an unregistered tool: " + call.name());
                        }
                    }
                    if (!streamed.get() && !turn.text().isEmpty()) emit(new AgentEvent.TextDelta(id, ++sequence, turn.text()));
                    token.check();
                    history.add(new Message.Assistant(turn.text(), turn.toolCalls(), turn.replay()));
                    if (turn.toolCalls().isEmpty()) {
                        result = new RunResult(turn.text(), history, turn.output());
                        break;
                    }
                    for (var call : turn.toolCalls()) history.add(executeTool(call));
                }
            } catch (Exception | Error error) {
                failure = consumerFailure == null ? error : consumerFailure;
            } finally {
                cancelled = token.isCancelled() || Thread.currentThread().isInterrupted();
                registration.close();
                try {
                    token.cancel();
                } catch (RuntimeException error) {
                    if (failure == null) failure = error;
                    else if (failure != error) failure.addSuppressed(error);
                }
            }
            if (failure instanceof InterruptedException) Thread.currentThread().interrupt();
            cancelled |= parent.isCancelled() || Thread.currentThread().isInterrupted();
            if (cancelled && consumerFailure == null && !(failure instanceof InterruptedException)) {
                var cancellation = new CancellationException("Run cancelled");
                if (failure != null) cancellation.initCause(failure);
                failure = cancellation;
            }
            if (failure != null) {
                if (consumerFailure == null) {
                    if (failure instanceof CancellationException || failure instanceof InterruptedException) {
                        emit(new AgentEvent.Aborted(id, ++sequence));
                    } else {
                        String message = failure.getMessage();
                        if (message == null || !Json.validText(message)) message = "Provider request failed";
                        emit(new AgentEvent.Failed(id, ++sequence, message));
                    }
                }
                rethrow(failure);
            }
            emit(new AgentEvent.Completed(id, ++sequence, Objects.requireNonNull(result).text()));
            parent.check();
            return result;
        }

        private void emit(AgentEvent event) throws Exception {
            if (consumerFailure != null) rethrow(consumerFailure);
            try {
                events.accept(event);
            } catch (Exception | Error error) {
                consumerFailure = error;
                throw error;
            }
        }

        private Message.ToolResult executeTool(ToolCall call) throws Exception {
            token.check();
            var arguments = Json.requireObject(call.arguments(), "tool arguments");
            emit(new AgentEvent.ToolStarted(id, ++sequence, call.callId(), call.name(), arguments));
            String output = "Tool cancelled.";
            boolean failed = true;
            try {
                output = tools.get(call.name()).call(arguments, token);
                token.check();
                Json.text(output, "tool output");
                if (output.getBytes(StandardCharsets.UTF_8).length > Tool.MAX_OUTPUT_BYTES) {
                    throw new IllegalArgumentException("Tool output exceeds " + Tool.MAX_OUTPUT_BYTES + " UTF-8 bytes.");
                }
                failed = false;
            } catch (CancellationException | InterruptedException error) {
                output = "Tool cancelled.";
                throw error;
            } catch (Exception error) {
                output = "Tool cancelled.";
                token.check();
                String detail = error.getMessage();
                if (detail == null || !Json.validText(detail)) detail = "Handler failed";
                if (detail.length() > 2000) {
                    int end = Character.isHighSurrogate(detail.charAt(1999)) ? 1999 : 2000;
                    detail = detail.substring(0, end);
                }
                output = "Tool failed: " + detail;
            } finally {
                emit(new AgentEvent.ToolEnded(id, ++sequence, call.callId(), call.name(), output, failed));
            }
            token.check();
            return new Message.ToolResult(call.callId(), call.name(), output, failed);
        }
    }

    private static void rethrow(Throwable error) throws Exception {
        if (error instanceof Exception exception) throw exception;
        if (error instanceof Error fatal) throw fatal;
        throw new AssertionError(error);
    }
}
