defmodule TinyAgent.AgentEvent do
  @moduledoc "An immutable run event with correlation and ordered protocol serialization."
  @enforce_keys [:run_id, :sequence, :kind]
  defstruct [:run_id, :sequence, :kind]

  def new(run_id, sequence, kind)
      when is_binary(run_id) and run_id != "" and is_integer(sequence) and sequence > 0 do
    event = %__MODULE__{run_id: run_id, sequence: sequence, kind: kind}

    unless String.valid?(run_id) and valid_kind?(kind),
      do: raise(ArgumentError, "Invalid event kind or UTF-8 text.")

    event
  end

  defp valid_kind?(kind) when kind in [:start, :aborted], do: true

  defp valid_kind?({kind, text}) when kind in [:text_delta, :completed, :failed],
    do: is_binary(text) and String.valid?(text)

  defp valid_kind?({:tool_start, id, name, arguments}),
    do: valid_tool?(id, name) and is_map(arguments) and not is_struct(arguments)

  defp valid_kind?({:tool_end, id, name, output, failed}),
    do:
      valid_tool?(id, name) and is_binary(output) and String.valid?(output) and is_boolean(failed)

  defp valid_kind?(_kind), do: false

  defp valid_tool?(id, name),
    do:
      is_binary(id) and id != "" and String.valid?(id) and is_binary(name) and name != "" and
        String.valid?(name)

  def to_map(%__MODULE__{} = event) do
    fields =
      case event.kind do
        :start ->
          %{"type" => "agent_start"}

        {:text_delta, text} when is_binary(text) ->
          %{"type" => "text_delta", "delta" => text}

        {:tool_start, id, name, arguments} ->
          %{
            "type" => "tool_execution_start",
            "toolCallId" => id,
            "toolName" => name,
            "arguments" => arguments
          }

        {:tool_end, id, name, output, failed} ->
          %{
            "type" => "tool_execution_end",
            "toolCallId" => id,
            "toolName" => name,
            "output" => output,
            "isError" => failed
          }

        {:completed, text} when is_binary(text) ->
          %{"type" => "agent_end", "status" => "completed", "text" => text}

        :aborted ->
          %{"type" => "agent_end", "status" => "aborted"}

        {:failed, message} when is_binary(message) ->
          %{
            "type" => "agent_end",
            "status" => "failed",
            "error" => %{
              "code" => "provider_error",
              "message" => message,
              "retryable" => false
            }
          }
      end

    Map.merge(fields, %{"runId" => event.run_id, "sequence" => event.sequence})
  end
end

defmodule TinyAgent.RunEvents do
  @moduledoc false
  alias TinyAgent.{AgentEvent, Cancelled}

  # Provider callbacks do not return updated state. A unique, temporary process
  # dictionary entry keeps synchronous delivery in the caller's process.
  def open(run_id, callback) do
    key = {__MODULE__, make_ref()}
    Process.put(key, %{sequence: 0, failure: nil})
    %{key: key, owner: self(), run_id: run_id, callback: callback}
  end

  def close(events), do: Process.delete(events.key)

  def emit(events, kind, token) do
    check!(events)
    state = state!(events)
    event = AgentEvent.new(events.run_id, state.sequence + 1, kind)
    Process.put(events.key, %{state | sequence: event.sequence})

    try do
      if events.callback, do: events.callback.(event, token)
      :ok
    catch
      kind, reason ->
        failure = {kind, reason, __STACKTRACE__}
        Process.put(events.key, %{state | sequence: event.sequence, failure: failure})
        :erlang.raise(kind, reason, __STACKTRACE__)
    end
  end

  def failure(events), do: state!(events).failure
  def sequence(events), do: state!(events).sequence

  def check!(events) do
    case failure(events) do
      nil -> :ok
      {kind, reason, trace} -> :erlang.raise(kind, reason, trace)
    end
  end

  defp state!(events) do
    unless self() == events.owner,
      do: raise(ArgumentError, "Provider callbacks must run in the calling process.")

    Process.get(events.key) || raise(Cancelled)
  end
end
