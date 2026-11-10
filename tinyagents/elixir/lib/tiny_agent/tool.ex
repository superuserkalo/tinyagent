defmodule TinyAgent.Tool do
  @moduledoc "An immutable definition, compiled argument schema, and native handler."
  alias TinyAgent.{CancellationToken, JSON, ToolDefinition, ToolSchema}
  @enforce_keys [:definition, :schema, :handler]
  defstruct [:definition, :schema, :handler]

  def new(name, description, parameters, handler) when is_function(handler, 2) do
    definition = ToolDefinition.new(name, description, parameters)

    unless String.valid?(name) and String.valid?(description),
      do: raise(ArgumentError, "Tool metadata must be UTF-8 text.")

    %__MODULE__{
      definition: definition,
      schema: ToolSchema.build(definition.parameters),
      handler: handler
    }
  end

  def call(%__MODULE__{} = tool, arguments, %CancellationToken{} = cancellation) do
    CancellationToken.check!(cancellation)
    arguments = JSON.normalize_object!(arguments)

    unless ToolSchema.valid?(tool.schema, arguments),
      do: raise(ArgumentError, "Invalid arguments for tool '#{tool.definition.name}'.")

    CancellationToken.check!(cancellation)
    tool.handler.(arguments, cancellation)
  end
end
