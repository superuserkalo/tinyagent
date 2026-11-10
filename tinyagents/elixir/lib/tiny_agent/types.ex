defmodule TinyAgent.ProviderError do
  defexception [:message, :cause]
end

defmodule TinyAgent.UserMessage do
  @enforce_keys [:text]
  defstruct [:text]
  def new(text) when is_binary(text), do: %__MODULE__{text: text}
end

defmodule TinyAgent.AssistantMessage do
  @enforce_keys [:text]
  defstruct [:text, tool_calls: [], replay: []]
end

defmodule TinyAgent.ToolResultMessage do
  @enforce_keys [:call_id, :name, :output]
  defstruct [:call_id, :name, :output, is_error: false]

  def new(call_id, name, output, is_error \\ false)
      when is_binary(call_id) and call_id != "" and is_binary(name) and name != "" and
             is_binary(output) and is_boolean(is_error),
      do: %__MODULE__{call_id: call_id, name: name, output: output, is_error: is_error}
end

defmodule TinyAgent.ToolDefinition do
  @moduledoc "Provider metadata. It does not validate schemas or execute handlers."
  @enforce_keys [:name, :description, :parameters]
  defstruct [:name, :description, :parameters]

  def new(name, description, parameters)
      when is_binary(name) and name != "" and is_binary(description) do
    parameters =
      if is_boolean(parameters),
        do: parameters,
        else: TinyAgent.JSON.normalize_object!(parameters)

    %__MODULE__{name: name, description: description, parameters: parameters}
  end
end

defmodule TinyAgent.ToolCall do
  @enforce_keys [:call_id, :name, :arguments]
  defstruct [:call_id, :name, :arguments]

  def new(call_id, name, arguments)
      when is_binary(call_id) and call_id != "" and is_binary(name) and name != "" do
    %__MODULE__{
      call_id: call_id,
      name: name,
      arguments: TinyAgent.JSON.normalize_object!(arguments)
    }
  end
end

defmodule TinyAgent.ProviderRequest do
  @enforce_keys [:model, :instructions, :history]
  defstruct [:model, :instructions, :history, tools: []]

  def new(model, instructions, history, tools \\ [])
      when is_binary(model) and model != "" and is_binary(instructions) and is_list(history) and
             is_list(tools) do
    unless Enum.all?(history, &message?/1),
      do: raise(ArgumentError, "Unsupported history message.")

    unless Enum.all?(tools, &is_struct(&1, TinyAgent.ToolDefinition)),
      do: raise(ArgumentError, "Expected tool definitions.")

    %__MODULE__{model: model, instructions: instructions, history: history, tools: tools}
  end

  defp message?(%TinyAgent.UserMessage{}), do: true
  defp message?(%TinyAgent.AssistantMessage{}), do: true
  defp message?(%TinyAgent.ToolResultMessage{}), do: true
  defp message?(_message), do: false
end

defmodule TinyAgent.ProviderTurn do
  @enforce_keys [:text, :tool_calls, :replay, :output, :assistant]
  defstruct [:text, :tool_calls, :replay, :output, :assistant]

  def new(text, calls, replay, output) do
    %__MODULE__{
      text: text,
      tool_calls: calls,
      replay: replay,
      output: output,
      assistant: %TinyAgent.AssistantMessage{text: text, tool_calls: calls, replay: replay}
    }
  end
end
