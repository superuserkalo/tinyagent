defmodule TinyAgent.RpcSession do
  @moduledoc "Parsed RPC commands and committed session history; byte transport remains caller-owned."
  alias TinyAgent.{Agent, AgentEvent, Session, SessionError}
  @enforce_keys [:session, :emit]
  defstruct [:session, :emit]

  def new(%Agent{} = agent, emit) when is_function(emit, 2) do
    {:ok, session} = Session.start_link(agent)
    %__MODULE__{session: session, emit: emit}
  end

  def handle(%__MODULE__{} = rpc, command) do
    cond do
      not is_map(command) or is_struct(command) ->
        error(rpc, nil, nil, "invalid_command", "RPC command must be an object")

      not valid_json?(command) ->
        error(rpc, nil, nil, "invalid_json", "RPC input is not valid JSON")

      Map.has_key?(command, "id") and not is_binary(command["id"]) ->
        error(rpc, nil, nil, "invalid_command", "command.id must be a string")

      true ->
        dispatch(rpc, command)
    end

    :ok
  end

  def get_state(%__MODULE__{} = rpc) do
    state = Session.state(rpc.session)

    %{
      "protocolVersion" => 1,
      "status" => if(state.run_id, do: "running", else: "idle"),
      "runId" => state.run_id,
      "messageCount" => state.message_count
    }
  end

  def cancel(%__MODULE__{} = rpc), do: Session.cancel(rpc.session)
  def stop(%__MODULE__{} = rpc), do: Session.stop(rpc.session)
  def wait_idle(%__MODULE__{} = rpc), do: Session.wait_idle(rpc.session)

  def close(%__MODULE__{} = rpc) do
    monitor = Process.monitor(rpc.session)

    try do
      waited =
        try do
          Session.close(rpc.session)
        rescue
          error in SessionError ->
            if error.reason == :closed, do: true, else: reraise(error, __STACKTRACE__)
        end

      if waited do
        receive do
          {:DOWN, ^monitor, :process, _, _} -> :ok
        end
      end

      waited
    after
      Process.demonitor(monitor, [:flush])
    end
  end

  defp dispatch(rpc, command) do
    id = command["id"]
    type = if is_binary(command["type"]), do: command["type"], else: "unknown"

    case execute(rpc, command, id, type) do
      :started -> :ok
      {:reply, data} -> rpc.emit.(response(id, type, data), nil)
      {:error, code, message} -> error(rpc, id, type, code, message)
    end
  end

  defp execute(rpc, command, id, type) do
    case type do
      "prompt" ->
        prompt = command["message"]

        if is_binary(prompt) and prompt != "" do
          Session.start(
            rpc.session,
            prompt,
            fn run_id, token -> rpc.emit.(response(id, type, %{"runId" => run_id}), token) end,
            fn event, token -> rpc.emit.(AgentEvent.to_map(event), token) end
          )

          :started
        else
          {:error, "invalid_command", "prompt.message must be a non-empty string"}
        end

      "abort" ->
        run_id = cancel(rpc)
        {:reply, %{"accepted" => run_id != nil, "runId" => run_id}}

      "new_session" ->
        aborted = Session.reset(rpc.session)
        {:reply, %{"abortedRun" => aborted}}

      "get_state" ->
        {:reply, get_state(rpc)}

      _ ->
        {:error, "invalid_command", "Unknown command type"}
    end
  rescue
    exception in SessionError ->
      code = if exception.reason == :busy, do: "busy", else: "session_closed"
      {:error, code, exception.message}
  end

  defp response(id, command, data) do
    value = %{"type" => "response", "command" => command, "success" => true, "data" => data}
    if id == nil, do: value, else: Map.put(value, "id", id)
  end

  defp error(rpc, id, command, code, message) do
    value = %{"type" => "protocol_error", "error" => %{"code" => code, "message" => message}}

    value =
      if id == nil,
        do: value,
        else:
          Map.merge(
            value,
            %{"type" => "response", "id" => id, "command" => command, "success" => false}
          )

    rpc.emit.(value, nil)
  end

  defp valid_json?(value, depth \\ 0)
  defp valid_json?(_value, depth) when depth > 100, do: false
  defp valid_json?(value, _depth) when is_binary(value), do: String.valid?(value)

  defp valid_json?(value, _depth) when is_number(value) or is_boolean(value) or is_nil(value),
    do: true

  defp valid_json?(value, depth) when is_list(value), do: valid_array?(value, depth)

  defp valid_json?(value, depth) when is_map(value) and not is_struct(value),
    do:
      Enum.all?(value, fn {key, item} ->
        is_binary(key) and String.valid?(key) and valid_json?(item, depth + 1)
      end)

  defp valid_json?(_value, _depth), do: false

  defp valid_array?([], _depth), do: true

  defp valid_array?([head | tail], depth),
    do: valid_json?(head, depth + 1) and valid_array?(tail, depth)

  defp valid_array?(_tail, _depth), do: false
end
