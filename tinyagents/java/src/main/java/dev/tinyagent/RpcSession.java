package dev.tinyagent;

import java.util.Objects;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

/** JSON command mapping over one native session. The emitter owns delivery. */
public final class RpcSession implements AutoCloseable {
    @FunctionalInterface
    public interface Emitter {
        /** Non-null cancellation marks a run acknowledgement or event; await its delivery. */
        void accept(ObjectNode value, CancellationToken cancellation) throws Exception;
    }

    private final Session session;
    private final Emitter emit;

    public RpcSession(Agent agent, Emitter emit) {
        this.session = new Session(agent);
        this.emit = Objects.requireNonNull(emit);
    }

    public void handle(JsonNode request) throws Exception {
        if (request == null || !request.isObject()) {
            commandError(null, null, "invalid_command", "RPC command must be an object");
            return;
        }
        request = request.deepCopy();
        try { RpcProtocol.validateText(request); }
        catch (IllegalArgumentException failure) {
            commandError(null, null, "invalid_json", "RPC input is not valid JSON");
            return;
        }
        if (request.has("id") && !request.path("id").isString()) {
            commandError(null, null, "invalid_command", "command.id must be a string");
            return;
        }
        String id = request.has("id") ? request.path("id").stringValue() : null;
        String command = request.path("type").isString() ? request.path("type").stringValue() : "unknown";
        try {
            switch (command) {
                case "prompt" -> {
                    var message = request.path("message");
                    if (!message.isString() || message.stringValue().isEmpty()) {
                        commandError(id, command, "invalid_command", "prompt.message must be a non-empty string");
                        return;
                    }
                    session.start(message.stringValue(), (runId, token) ->
                            emit.accept(response(id, command, Json.MAPPER.createObjectNode().put("runId", runId)), token),
                            (event, token) -> emit.accept(event.toJson(), token));
                }
                case "abort" -> {
                    String runId = session.cancel();
                    emit.accept(response(id, command, Json.MAPPER.createObjectNode()
                            .put("accepted", runId != null).put("runId", runId)), null);
                }
                case "new_session" -> emit.accept(response(id, command, Json.MAPPER.createObjectNode()
                        .put("abortedRun", session.reset())), null);
                case "get_state" -> emit.accept(response(id, command, state()), null);
                default -> commandError(id, command, "invalid_command", "Unknown command type");
            }
        } catch (Session.BusyException failure) {
            commandError(id, command, "busy", failure.getMessage());
        } catch (Session.ClosedException failure) {
            commandError(id, command, "session_closed", failure.getMessage());
        }
    }

    public ObjectNode state() {
        var state = session.state();
        return Json.MAPPER.createObjectNode().put("protocolVersion", 1)
                .put("status", state.running() ? "running" : "idle").put("runId", state.runId())
                .put("messageCount", state.messageCount());
    }

    public String cancel() { return session.cancel(); }
    public String stop() { return session.stop(); }
    public boolean awaitIdle() { return session.awaitIdle(); }
    @Override public void close() { session.close(); }

    private static ObjectNode response(String id, String command, ObjectNode data) {
        var value = Json.object("response").put("command", command).put("success", true).set("data", data);
        if (id != null) value.put("id", id);
        return value;
    }

    private void commandError(String id, String command, String code, String message) throws Exception {
        var value = RpcProtocol.error(code, message);
        if (id != null) value.put("id", id).put("type", "response").put("command", command).put("success", false);
        emit.accept(value, null);
    }
}
