defmodule TinyAgent.Rpc do
  @moduledoc "TinyAgent JSONL RPC over exclusively borrowed POSIX descriptors."
  alias TinyAgent.{Agent, CancellationToken, RpcIO, RpcInput, RpcOutput, RpcProtocol, RpcSession}

  @doc "Serves stdin/stdout, temporarily handles SIGINT, and waits for cooperative cleanup."
  def run_stdio(%Agent{} = agent, options \\ []) do
    options = Keyword.validate!(options, cancellation: CancellationToken.new())
    run(agent, Keyword.merge(options, input: 0, output: 1, handle_interrupt: true))
  end

  @doc """
  Serves JSONL over distinct `input` and `output` POSIX descriptors.

  Descriptors remain caller-owned and must be borrowed exclusively until return.
  Optional `cancellation` raises `Cancelled` after cleanup; `handle_interrupt`
  defaults to false. EOF or handled SIGINT returns `:ok`. I/O failures raise
  `RpcError` after the reader, writer, session, and active run have stopped.
  """
  def run(%Agent{} = agent, options) do
    options =
      Keyword.validate!(options, [
        :input,
        :output,
        cancellation: CancellationToken.new(),
        handle_interrupt: false
      ])

    input = Keyword.fetch!(options, :input)
    output = Keyword.fetch!(options, :output)

    unless is_integer(input) and input >= 0 and is_integer(output) and output >= 0 and
             input != output,
           do:
             raise(ArgumentError, "RPC input and output must be distinct nonnegative descriptors")

    unless is_boolean(options[:handle_interrupt]),
      do: raise(ArgumentError, "handle_interrupt must be a boolean")

    cancellation = options[:cancellation]
    CancellationToken.check!(cancellation)

    signal =
      if options[:handle_interrupt] do
        case RpcIO.watch_interrupt() do
          {:ok, signal} -> signal
          {:error, message} -> raise RpcProtocol.error("signal_error", message)
        end
      end

    try do
      stopping = CancellationToken.new()

      case RpcOutput.start_link(output, stopping) do
        {:error, error} ->
          raise error

        {:ok, writer} ->
          result =
            capture(fn -> session(agent, input, writer, stopping, cancellation, signal) end)

          CancellationToken.cancel(stopping)
          RpcOutput.cancel(writer)
          output_result = RpcOutput.finish(writer)
          flush_output(writer)

          case result do
            {:raised, kind, reason, stack} ->
              :erlang.raise(kind, reason, stack)

            :ok ->
              if match?({:error, _}, output_result), do: raise(elem(output_result, 1))
              CancellationToken.check!(cancellation)
              :ok
          end
      end
    after
      if signal, do: RpcIO.close(signal)
    end
  end

  defp session(agent, input, writer, stopping, cancellation, signal) do
    session = RpcSession.new(agent, fn value, token -> RpcOutput.emit(writer, value, token) end)
    owner = self()

    try do
      {reader, monitor} =
        spawn_monitor(fn ->
          result = capture(fn -> RpcInput.run(input, session, writer, stopping) end)
          send(owner, {:rpc_reader_result, self(), result})
        end)

      await_reader(%{
        reader: reader,
        monitor: monitor,
        writer: writer,
        session: session,
        stopping: stopping,
        cancellation: cancellation,
        signal: signal,
        result: nil,
        stopped: false
      })
    after
      CancellationToken.cancel(stopping)
      RpcSession.stop(session)
      RpcOutput.cancel(writer)
      RpcSession.close(session)
    end
  end

  defp await_reader(state) do
    %{reader: reader, monitor: monitor, writer: writer} = state

    cancelled =
      CancellationToken.cancelled?(state.stopping) or
        CancellationToken.cancelled?(state.cancellation) or
        (state.signal != nil and RpcIO.interrupted?(state.signal))

    if cancelled and not state.stopped do
      CancellationToken.cancel(state.stopping)
      RpcSession.stop(state.session)
      RpcOutput.cancel(writer)
    end

    state = %{state | stopped: state.stopped or cancelled}

    receive do
      {:rpc_reader_result, ^reader, next} ->
        await_reader(%{state | result: next})

      {:DOWN, ^monitor, :process, ^reader, reason} ->
        case {reason, state.result} do
          {:normal, :ok} -> :ok
          {:normal, {:raised, kind, error, stack}} -> :erlang.raise(kind, error, stack)
          _ -> raise RpcProtocol.error("input_error", "RPC reader stopped: #{inspect(reason)}")
        end

      {:rpc_output_failed, ^writer, _error} ->
        await_reader(state)
    after
      10 -> await_reader(state)
    end
  end

  defp capture(callback) do
    callback.()
    :ok
  catch
    kind, reason -> {:raised, kind, reason, __STACKTRACE__}
  end

  defp flush_output(writer) do
    receive do
      {:rpc_output_failed, ^writer, _} -> flush_output(writer)
    after
      0 -> :ok
    end
  end
end
