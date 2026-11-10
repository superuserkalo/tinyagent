defmodule TinyAgent.RpcIOFixture do
  alias TinyAgent.RpcIO

  def run([mode | descriptors]) do
    case mode do
      kind when kind in ["echo", "files"] ->
        [input_fd, output_fd] =
          if kind == "echo", do: [0, 1], else: Enum.map(descriptors, &String.to_integer/1)

        {:ok, input} = RpcIO.open(input_fd, 0)
        {:ok, output} = RpcIO.open(output_fd, 1)
        echo(input, output)
        :ok = RpcIO.close(input)
        :ok = RpcIO.close(output)

      "stalled" ->
        {:ok, output} = RpcIO.open(1, 1)
        fill(output, String.duplicate("x", 1_048_576), 0)
        :ok = RpcIO.close(output)
        :ok = RpcIO.close(output)
        {:error, _} = RpcIO.write(output, "closed", 0)
        IO.puts(:stderr, "closed")

      "broken" ->
        {:ok, output} = RpcIO.open(1, 1)
        IO.puts(:stderr, "ready")
        "x" = IO.binread(:stdio, 1)
        {:error, "Broken pipe"} = RpcIO.write(output, "broken", 0)
        :ok = RpcIO.close(output)
        IO.puts(:stderr, "closed")

      "owner" ->
        owner_exit(Enum.map(descriptors, &String.to_integer/1))

      "interrupt" ->
        {:ok, signal} = RpcIO.watch_interrupt()
        false = RpcIO.interrupted?(signal)
        {:error, _} = RpcIO.watch_interrupt()
        IO.puts(:stderr, "ready")
        await_interrupt(signal)
        :ok = RpcIO.close(signal)
        :ok = RpcIO.close(signal)
        {:ok, next} = RpcIO.watch_interrupt()
        false = RpcIO.interrupted?(next)
        :ok = RpcIO.close(next)
        IO.puts(:stderr, "restored")
        "x" = IO.binread(:stdio, 1)

      "signal_owner" ->
        parent = self()

        {owner, monitor} =
          spawn_monitor(fn ->
            {:ok, signal} = RpcIO.watch_interrupt()
            send(parent, {:retained, signal})
            receive do: (:stop -> :ok)
          end)

        receive do
          {:retained, signal} ->
            send(owner, :stop)
            receive do: ({:DOWN, ^monitor, :process, ^owner, :normal} -> :ok)
            # The resource remains reachable; its owner monitor must release it.
            assert_foreign(fn -> RpcIO.interrupted?(signal) end)
        end

        next = after_owner_exit(System.monotonic_time(:millisecond) + 1_000)
        :ok = RpcIO.close(next)
        IO.puts(:stderr, "restored")
        "x" = IO.binread(:stdio, 1)

      "invalid" ->
        assert_foreign(fn -> RpcIO.open(-1, 0) end)
        assert_foreign(fn -> RpcIO.open(0, 2) end)
        {:error, _} = RpcIO.open(1, 0)
        {:error, _} = RpcIO.open(0, 1)
        {:error, _} = RpcIO.open(2_000_000_000, 0)
        {:ok, input} = RpcIO.open(0, 0)
        assert_foreign(fn -> RpcIO.write(input, "", 0) end)
        :ok = RpcIO.close(input)
        {:error, _} = RpcIO.read(input)
        IO.puts(:stderr, "checked")
    end
  end

  defp echo(input, output) do
    case RpcIO.read(input) do
      :again ->
        Process.sleep(1)
        echo(input, output)

      :eof ->
        :ok

      {:ok, bytes} ->
        true = byte_size(bytes) <= 8192
        write(output, bytes, 0)
        echo(input, output)
    end
  end

  defp write(_output, bytes, offset) when offset == byte_size(bytes), do: :ok

  defp write(output, bytes, offset) do
    case RpcIO.write(output, bytes, offset) do
      :again ->
        Process.sleep(1)
        write(output, bytes, offset)

      {:ok, count} ->
        true = count > 0 and count <= 65_536
        write(output, bytes, offset + count)
    end
  end

  defp fill(output, bytes, offset) do
    case RpcIO.write(output, bytes, offset) do
      :again -> IO.puts(:stderr, "stalled:#{offset}")
      {:ok, count} -> fill(output, bytes, offset + count)
    end
  end

  defp owner_exit([input_fd, output_fd]) do
    parent = self()

    {owner, monitor} =
      spawn_monitor(fn ->
        {:ok, input} = RpcIO.open(input_fd, 0)
        {:ok, output} = RpcIO.open(output_fd, 1)
        send(parent, {:handles, input, output})
        receive do: (:stop -> :ok)
      end)

    receive do
      {:handles, input, output} ->
        assert_foreign(fn -> RpcIO.read(input) end)
        assert_foreign(fn -> RpcIO.write(output, "foreign", 0) end)
        assert_foreign(fn -> RpcIO.close(output) end)
        IO.puts(:stderr, "ready")
        "x" = IO.binread(:stdio, 1)
        send(owner, :stop)
        receive do: ({:DOWN, ^monitor, :process, ^owner, :normal} -> :ok)
        IO.puts(:stderr, "restored")
        "x" = IO.binread(:stdio, 1)
        assert_foreign(fn -> RpcIO.read(input) end)
        assert_foreign(fn -> RpcIO.close(output) end)
    end
  end

  defp await_interrupt(signal) do
    unless RpcIO.interrupted?(signal) do
      Process.sleep(1)
      await_interrupt(signal)
    end
  end

  defp assert_foreign(callback) do
    try do
      callback.()
      raise "Invalid native operation succeeded"
    rescue
      ArgumentError -> :ok
    end
  end

  defp after_owner_exit(deadline) do
    case RpcIO.watch_interrupt() do
      {:ok, signal} ->
        signal

      {:error, "An RPC connection already owns SIGINT"} ->
        if System.monotonic_time(:millisecond) >= deadline,
          do: raise("Signal owner was not released")

        Process.sleep(1)
        after_owner_exit(deadline)
    end
  end
end

TinyAgent.RpcIOFixture.run(System.argv())
