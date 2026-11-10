defmodule TinyAgent.HttpConnection do
  @moduledoc false
  alias TinyAgent.{AgentEvent, CancellationToken, HttpTransport, Session, SessionError}
  @line_limit 1_048_576

  def stream(conn, session, prompt) do
    owner = self()

    id =
      Session.start(
        session,
        prompt,
        fn _id, token ->
          Process.put(__MODULE__, token)
          exchange(owner, {:begin, token})
        end,
        fn event, token ->
          if event.kind == :aborted, do: CancellationToken.cancel(Process.get(__MODULE__))
          exchange(owner, {:event, event, token})
        end
      )

    receive do
      {:"$gen_call", from, {__MODULE__, {:begin, token}}} ->
        socket = HttpTransport.socket()
        {worker, _} = from
        monitor = Process.monitor(worker)
        guard = spawn(fn -> watch(socket, owner, token, Process.monitor(owner), nil) end)

        try do
          conn =
            writing(guard, token, false, fn ->
              conn
              |> Plug.Conn.put_resp_header("connection", "close")
              |> Plug.Conn.put_resp_content_type("application/jsonl")
              |> Plug.Conn.send_chunked(200)
            end)

          GenServer.reply(from, :ok)
          deliver(conn, monitor, guard)
        catch
          _kind, _reason ->
            CancellationToken.cancel(token)
            :gen_tcp.close(socket)
            # Also releases a worker whose header acknowledgement failed.
            GenServer.reply(from, {:error, "HTTP output failed"})
            %{conn | state: :sent}
        after
          stop_guard(guard)
          Process.demonitor(monitor, [:flush])

          try do
            Session.wait_idle(session, id)
          rescue
            SessionError -> :ok
          end
        end
    end
  end

  defp exchange(owner, message) do
    case GenServer.call(owner, {__MODULE__, message}, :infinity) do
      :ok -> :ok
      {:error, message} -> raise message
    end
  catch
    :exit, _ -> raise "HTTP connection closed"
  end

  defp deliver(conn, monitor, guard) do
    receive do
      {:"$gen_call", from, {__MODULE__, {:event, event, token}}} ->
        {conn, terminal} = write_event(conn, from, event, token, guard)
        if terminal, do: conn, else: deliver(conn, monitor, guard)

      {:DOWN, ^monitor, :process, _, _} ->
        :gen_tcp.close(HttpTransport.socket())
        %{conn | state: :sent}
    end
  end

  defp write_event(conn, from, event, token, guard) do
    value = AgentEvent.to_map(event)
    bytes = Jason.encode!(value)
    overflow = byte_size(bytes) > @line_limit
    terminal = value["type"] == "agent_end" or overflow

    bytes =
      if overflow do
        Jason.encode!(%{
          "type" => "agent_end",
          "runId" => event.run_id,
          "sequence" => event.sequence,
          "status" => "failed",
          "error" => %{
            "code" => "output_too_large",
            "message" => "Event exceeds 1048576 UTF-8 bytes",
            "retryable" => false
          }
        })
      else
        bytes
      end

    conn =
      writing(guard, token, terminal, fn ->
        {:ok, conn} = Plug.Conn.chunk(conn, bytes <> "\n")

        if terminal do
          # Plug.chunk skips empty data. The adapter's empty chunk closes the
          # HTTP response; await it before the session can commit history.
          {adapter, state} = conn.adapter
          {:ok, _, state} = adapter.chunk(state, "")
          %{conn | adapter: {adapter, state}, state: :sent}
        else
          conn
        end
      end)

    GenServer.reply(
      from,
      if(overflow, do: {:error, "Event exceeds output limit"}, else: :ok)
    )

    {conn, terminal}
  catch
    kind, reason ->
      GenServer.reply(from, {:error, "HTTP output failed"})
      :erlang.raise(kind, reason, __STACKTRACE__)
  end

  defp writing(guard, token, terminal, write) do
    exchange(guard, {:writing, token, terminal})
    result = write.()
    exchange(guard, if(terminal, do: :finished, else: :written))
    result
  end

  # The body is fully consumed before this reader starts. It discards later
  # input and detects EOF even while the connection owner is blocked writing.
  # Each write supplies its own callback token, which may cancel before its parent.
  defp watch(socket, owner, parent, monitor, pending) do
    now = System.monotonic_time(:millisecond)

    pending =
      case pending do
        {token, deadline, terminal} ->
          deadline = deadline || if(CancellationToken.cancelled?(token), do: now + 1_000)
          {token, deadline, terminal}

        nil ->
          nil
      end

    expired =
      case pending do
        {_, deadline, _} when deadline != nil -> now >= deadline
        _ -> false
      end

    connected =
      case :gen_tcp.recv(socket, 0, 0) do
        {:ok, _data} -> true
        {:error, :timeout} -> true
        {:error, _} -> false
      end

    cond do
      not expired and not connected and match?({_, _, true}, pending) ->
        # EOF can arrive after the final native write succeeded. Closing the
        # socket releases an unfinished write; its owner decides whether that
        # write succeeded before acknowledging completion or cancelling the run.
        :gen_tcp.close(socket)
        await_final(owner, parent, monitor)

      expired or not connected ->
        CancellationToken.cancel(parent)
        :gen_tcp.close(socket)

      true ->
        receive do
          :finish ->
            :ok

          {:DOWN, ^monitor, :process, ^owner, _} ->
            CancellationToken.cancel(parent)
            :gen_tcp.close(socket)

          {:"$gen_call", from, {__MODULE__, {:writing, token, terminal}}} ->
            GenServer.reply(from, :ok)
            watch(socket, owner, parent, monitor, {token, nil, terminal})

          {:"$gen_call", from, {__MODULE__, :written}} ->
            GenServer.reply(from, :ok)
            watch(socket, owner, parent, monitor, nil)

          {:"$gen_call", from, {__MODULE__, :finished}} ->
            GenServer.reply(from, :ok)
        after
          50 -> watch(socket, owner, parent, monitor, pending)
        end
    end
  end

  defp await_final(owner, parent, monitor) do
    receive do
      :finish -> :ok
      {:DOWN, ^monitor, :process, ^owner, _} -> CancellationToken.cancel(parent)
      {:"$gen_call", from, {__MODULE__, :finished}} -> GenServer.reply(from, :ok)
    end
  end

  defp stop_guard(pid) do
    monitor = Process.monitor(pid)
    send(pid, :finish)

    receive do
      {:DOWN, ^monitor, :process, ^pid, _} -> :ok
    end
  end
end
