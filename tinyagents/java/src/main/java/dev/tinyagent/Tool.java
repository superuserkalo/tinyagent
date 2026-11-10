package dev.tinyagent;

import java.util.Objects;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

/** An immutable definition, compiled argument schema, and native handler. */
public final class Tool {
    public static final int MAX_OUTPUT_BYTES = 131_072;

    @FunctionalInterface
    public interface Handler {
        String execute(ObjectNode arguments, CancellationToken cancellation) throws Exception;
    }

    private final ToolDefinition definition;
    private final ToolSchema schema;
    private final Handler handler;

    public Tool(String name, String description, JsonNode parameters, Handler handler) {
        this.definition = new ToolDefinition(name, description, parameters);
        if (name.isEmpty()) throw new IllegalArgumentException("Tool name must be nonempty");
        this.schema = new ToolSchema(definition.parameters());
        this.handler = Objects.requireNonNull(handler);
    }

    public ToolDefinition definition() { return definition; }

    public String call(ObjectNode arguments, CancellationToken cancellation) throws Exception {
        cancellation.check();
        ObjectNode snapshot = Objects.requireNonNull(arguments).deepCopy();
        if (!schema.valid(snapshot)) {
            throw new IllegalArgumentException("Invalid arguments for tool '" + definition.name() + "'.");
        }
        cancellation.check();
        return handler.execute(snapshot, cancellation);
    }
}
