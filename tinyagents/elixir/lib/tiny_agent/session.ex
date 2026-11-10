defmodule TinyAgent.SessionError do
  @moduledoc false
  defexception [:reason, :message]
end

defmodule TinyAgent.Session do
  @moduledoc false
  use GenServer, restart: :temporary, shutdown: :infinity
  alias TinyAgent.{Agent, CancellationToken, SessionError}

  def start_link(%Agent{} = agent), do: GenServer.start_link(__MODULE__, agent)

  def start(session, prompt, before_run, on_event),
    do: call(session, {:start, prompt, before_run, on_event})

  def state(session), do: call(session, :state)
  def cancel(session), do: call(session, :cancel)
  def reset(session), do: call(session, :reset)
  def stop(session), do: call(session, :stop)
  def close(session), do: call(session, :close)

  def wait_idle(session, expected_run_id \\ nil),
    do: call(session, {:wait_idle, expected_run_id})

  defp call(session, request) do
    case GenServer.call(session, request, :infinity) do
      {:error, :closed} ->
        raise SessionError, reason: :closed, message: "Session is closed"

      {:error, :busy} ->
        raise SessionError, reason: :busy, message: "A run or reset is already active"

      value ->
        value
    end
  catch
    :exit, {reason, {GenServer, :call, _}} when reason in [:noproc, :normal, :shutdown] ->
      raise SessionError, reason: :closed, message: "Session is closed"
  end

  @impl true
  def init(agent) do
    Process.flag(:trap_exit, true)

    {:ok,
     %{
       agent: agent,
       history: [],
       active: nil,
       resetting: false,
       closed: false,
       closing: false,
       waiters: []
     }}
  end

  @impl true
  def handle_call({:start, _, _, _}, _from, %{closed: true} = state),
    do: {:reply, {:error, :closed}, state}

  def handle_call({:start, _, _, _}, _from, %{active: active, resetting: resetting} = state)
      when active != nil or resetting,
      do: {:reply, {:error, :busy}, state}

  def handle_call({:start, prompt, before_run, on_event}, _from, state) do
    id = "r_" <> Base.encode16(:crypto.strong_rand_bytes(12), case: :lower)
    token = CancellationToken.new()
    owner = self()

    {pid, monitor} =
      spawn_monitor(fn ->
        try do
          before_run.(id, token)

          result =
            Agent.run(state.agent, prompt,
              history: state.history,
              run_id: id,
              cancellation: token,
              on_event: on_event
            )

          send(owner, {:session_result, self(), result.history})
        catch
          _kind, _reason -> :ok
        end
      end)

    active = %{id: id, token: token, pid: pid, monitor: monitor, result: nil}
    {:reply, id, %{state | active: active}}
  end

  def handle_call(:state, _from, state),
    do: {:reply, %{run_id: run_id(state), message_count: length(state.history)}, state}

  def handle_call(:cancel, _from, state) do
    cancel_run(state)
    {:reply, run_id(state), state}
  end

  def handle_call(:stop, _from, state) do
    cancel_run(state)
    {:reply, run_id(state), %{state | closed: true}}
  end

  def handle_call(:reset, _from, %{closed: true} = state),
    do: {:reply, {:error, :closed}, state}

  def handle_call(:reset, _from, %{resetting: true} = state),
    do: {:reply, {:error, :busy}, state}

  def handle_call(:reset, from, state) do
    cancel_run(state)
    state = %{state | history: [], resetting: state.active != nil}
    await_run(state, from, state.active != nil, state.active != nil)
  end

  def handle_call({:wait_idle, expected}, from, state) do
    if expected == nil or expected == run_id(state),
      do: await_run(state, from, true),
      else: {:reply, true, state}
  end

  def handle_call(:close, from, state) do
    cancel_run(state)
    state = %{state | closed: true, closing: true}

    if state.active == nil,
      do: {:stop, :normal, true, state},
      else: await_run(state, from, true)
  end

  defp await_run(state, from, response, self_response \\ false)

  defp await_run(%{active: nil} = state, _from, response, _self_response),
    do: {:reply, response, state}

  defp await_run(%{active: %{pid: pid}} = state, {pid, _}, _response, self_response),
    do: {:reply, self_response, state}

  defp await_run(state, from, response, _self_response),
    do: {:noreply, %{state | waiters: [{from, response} | state.waiters]}}

  @impl true
  def handle_info({:session_result, pid, history}, %{active: %{pid: pid} = active} = state),
    do: {:noreply, %{state | active: %{active | result: history}}}

  def handle_info(
        {:DOWN, monitor, :process, pid, _reason},
        %{active: %{pid: pid, monitor: monitor} = active} = state
      ) do
    history =
      if active.result != nil and not CancellationToken.cancelled?(active.token) and
           not state.closed and not state.resetting,
         do: active.result,
         else: state.history

    CancellationToken.cancel(active.token)
    for {from, response} <- state.waiters, do: GenServer.reply(from, response)
    state = %{state | active: nil, history: history, resetting: false, waiters: []}
    if state.closing, do: {:stop, :normal, state}, else: {:noreply, state}
  end

  def handle_info(_message, state), do: {:noreply, state}

  @impl true
  def terminate(_reason, state) do
    # Parent or supervisor shutdown also owns cooperative worker cleanup.
    cancel_run(state)

    if state.active do
      monitor = state.active.monitor
      pid = state.active.pid

      receive do
        {:DOWN, ^monitor, :process, ^pid, _} -> :ok
      end
    end

    :ok
  end

  defp cancel_run(%{active: nil}), do: :ok
  defp cancel_run(%{active: active}), do: CancellationToken.cancel(active.token)
  defp run_id(%{active: nil}), do: nil
  defp run_id(%{active: active}), do: active.id
end
