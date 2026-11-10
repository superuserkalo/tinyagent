defmodule TinyAgent.RunResult do
  @moduledoc "Completed text, immutable history, and the provider's final output."
  alias TinyAgent.{AssistantMessage, JSON, ToolResultMessage, UserMessage}
  @enforce_keys [:text, :history, :output]
  defstruct [:text, :history, :output]

  def new(text, history, output) when is_binary(text) do
    unless String.valid?(text), do: raise(ArgumentError, "Final text must be valid UTF-8.")

    unless is_list(history) and
             Enum.all?(history, fn message ->
               is_struct(message, UserMessage) or is_struct(message, AssistantMessage) or
                 is_struct(message, ToolResultMessage)
             end),
           do: raise(ArgumentError, "Unsupported history message.")

    output = output |> JSON.list!("output") |> Enum.map(&JSON.normalize_object!/1)
    %__MODULE__{text: text, history: history, output: output}
  end
end
