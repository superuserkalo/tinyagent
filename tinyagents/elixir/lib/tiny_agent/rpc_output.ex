defmodule TinyAgent.RpcOutput do
  @moduledoc false
  use GenServer, restart: :temporary, shutdown: :infinity
  alias TinyAgent.{CancellationToken, RpcIO, RpcProtocol}

  def start_link(descriptor, stopping) do
    {:ok, writer} = GenServer.start_link(__MODULE__, {stopping, self()})
    monitor = Process.monitor(writer)

    try do
      case GenServer.call(writer, {:open, descriptor}, :infinity) do
        :ok ->
          {:ok, writer}

        {:error, error} ->
          receive do: ({:DOWN, ^monitor, :process, ^writer, _} -> {:error, error})
      end
    after
      Process.demonitor(monitor, [:flush])
    end
  end

  def emit(output, value, token \\ nil) do
    {bytes, error} = RpcProtocol.frame(value)

    case GenServer.call(output, {:enqueue, bytes, error, token}, :infinity) do
      :ok -> :ok
      {:error, error} -> raise error
    end
  end

  def cancel(output), do: GenServer.call(output, :cancel, :infinity)

  def finish(output) do
    monitor = Process.monitor(output)

    try do
      result = GenServer.call(output, :finish, :infinity)
      receive do: ({:DOWN, ^monitor, :process, ^output, _} -> result)
    after
      Process.demonitor(monitor, [:flush])
    end
  end

  @impl true
  def init({stopping, owner}) do
    Process.flag(:trap_exit, true)

    {:ok,
     %{
       handle: nil,
       stopping: stopping,
       owner: owner,
       queue: :queue.new(),
       current: nil,
       bytes: 0,
       failure: nil,
       cancelled: false,
       finishing: nil,
       timer: nil
     }}
  end

  @impl true
  def handle_call({:open, descriptor}, _from, state) do
    case RpcIO.open(descriptor, 1) do
      {:ok, handle} ->
        {:reply, :ok, %{state | handle: handle}}

      {:error, message} ->
        {:stop, :normal, {:error, RpcProtocol.error("output_error", message)}, state}
    end
  end

  def handle_call({:enqueue, _, _, _}, _from, %{failure: error} = state) when error != nil,
    do: {:reply, {:error, error}, state}

  def handle_call({:enqueue, bytes, error, token}, from, state) do
    if state.bytes + byte_size(bytes) > RpcProtocol.queue_bytes() do
      overflow =
        RpcProtocol.error(
          "output_queue_full",
          "RPC output queue exceeds #{RpcProtocol.queue_bytes()} UTF-8 bytes"
        )

      state = fail(state, overflow, true)
      {:reply, {:error, overflow}, wake(state)}
    else
      if error != nil and token != nil, do: CancellationToken.cancel(token)

      frame = %{
        bytes: bytes,
        offset: 0,
        token: token,
        reply: if(token, do: from),
        error: error,
        deadline: nil
      }

      state = %{
        state
        | bytes: state.bytes + byte_size(bytes),
          queue: :queue.in(frame, state.queue)
      }

      unless token, do: GenServer.reply(from, if(error, do: {:error, error}, else: :ok))
      {:noreply, wake(state)}
    end
  end

  def handle_call(:cancel, _from, state), do: {:reply, :ok, wake(%{state | cancelled: true})}
  def handle_call(:finish, from, state), do: settle(%{state | finishing: from})

  @impl true
  def handle_info(:pump, state) do
    state = %{state | timer: nil}
    state = if state.current, do: state, else: take_next(state)
    if state.current, do: pump(state), else: settle(state)
  end

  defp take_next(state) do
    case :queue.out(state.queue) do
      {{:value, frame}, queue} -> %{state | current: frame, queue: queue}
      {:empty, _} -> state
    end
  end

  defp pump(state) do
    frame = state.current
    now = System.monotonic_time(:millisecond)

    cancelled =
      state.cancelled or (frame.token != nil and CancellationToken.cancelled?(frame.token))

    deadline = frame.deadline || if(cancelled, do: now + 1_000)
    state = %{state | current: %{frame | deadline: deadline}}

    if deadline != nil and now >= deadline do
      settle(
        fail(
          state,
          RpcProtocol.error("output_stalled", "Cancelled RPC output did not drain"),
          false
        )
      )
    else
      case RpcIO.write(state.handle, frame.bytes, frame.offset) do
        :again ->
          {:noreply, wake(state, 10)}

        {:error, message} ->
          settle(fail(state, RpcProtocol.error("output_error", message), false))

        {:ok, count} ->
          offset = frame.offset + count

          if offset == byte_size(frame.bytes) do
            reply(frame, if(frame.error, do: {:error, frame.error}, else: :ok))
            settle(%{state | current: nil, bytes: state.bytes - byte_size(frame.bytes)})
          else
            {:noreply, wake(%{state | current: %{state.current | offset: offset}})}
          end
      end
    end
  end

  defp fail(state, error, report) do
    error = state.failure || error
    CancellationToken.cancel(state.stopping)
    send(state.owner, {:rpc_output_failed, self(), error})
    if state.current, do: reply(state.current, {:error, error})
    for frame <- :queue.to_list(state.queue), do: reply(frame, {:error, error})

    # Keep an in-progress line intact before the overflow report; release its waiter now.
    current = if report and state.current, do: %{state.current | reply: nil}, else: nil
    {bytes, _} = RpcProtocol.frame(RpcProtocol.error_value(error))

    queue =
      if report,
        do:
          :queue.in(
            %{bytes: bytes, offset: 0, token: nil, reply: nil, error: nil, deadline: nil},
            :queue.new()
          ),
        else: :queue.new()

    size =
      if(current, do: byte_size(current.bytes), else: 0) +
        if(report, do: byte_size(bytes), else: 0)

    %{state | failure: error, cancelled: true, current: current, queue: queue, bytes: size}
  end

  defp reply(%{reply: nil}, _result), do: :ok
  defp reply(frame, result), do: GenServer.reply(frame.reply, result)

  defp wake(state, delay \\ 0)

  defp wake(%{timer: nil} = state, delay),
    do: %{state | timer: Process.send_after(self(), :pump, delay)}

  defp wake(state, _delay), do: state

  defp settle(%{bytes: 0, finishing: from} = state) when from != nil do
    GenServer.reply(from, if(state.failure, do: {:error, state.failure}, else: :ok))
    {:stop, :normal, state}
  end

  defp settle(%{bytes: 0} = state), do: {:noreply, state}
  defp settle(state), do: {:noreply, wake(state)}

  @impl true
  def terminate(_reason, state) do
    CancellationToken.cancel(state.stopping)
    error = state.failure || RpcProtocol.error("output_closed", "RPC output is closed")
    if state.current, do: reply(state.current, {:error, error})
    for frame <- :queue.to_list(state.queue), do: reply(frame, {:error, error})
    if state.timer, do: Process.cancel_timer(state.timer)
    if state.handle, do: RpcIO.close(state.handle)
  end
end
