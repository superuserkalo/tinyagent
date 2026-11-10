defmodule TinyAgent.BashFixture do
  alias TinyAgent.{Bash, CancellationToken, Cancelled, ProviderError}

  def run(mode, path) do
    path = String.replace(path, "'", "'\\''")
    command = "sleep 30 & child=$!; printf '%s %s' \"$$\" \"$child\" > '#{path}'; "

    case mode do
      "background" ->
        Bash.run(command <> "exit 0")

      "overflow" ->
        try do
          Bash.run(command <> "while :; do printf '%08192d' 0; done")
          raise "Overflow succeeded"
        rescue
          error in ProviderError ->
            true = String.contains?(error.message, "output exceeded")
        end

      "environment" ->
        "exit_code: 0\nstdout:\nhellostderr:\n" = Bash.run("printf hello")

      _ ->
        token = CancellationToken.new()
        owner = self()

        {worker, monitor} =
          spawn_monitor(fn ->
            result =
              try do
                Bash.run(command <> "wait", cancellation: token)
              rescue
                Cancelled -> :cancelled
              end

            send(owner, {:result, result})
          end)

        await_file(path, System.monotonic_time(:millisecond) + 2_000)

        if mode == "independent",
          do: true = String.contains?(Bash.run("printf independent"), "independent")

        IO.puts("ready")
        "c" = IO.binread(:stdio, 1)

        if mode == "owner" do
          Process.exit(worker, :kill)
          receive do: ({:DOWN, ^monitor, :process, ^worker, :killed} -> :ok)
        else
          CancellationToken.cancel(token)
          receive do: ({:result, :cancelled} -> :ok)
          receive do: ({:DOWN, ^monitor, :process, ^worker, :normal} -> :ok)
        end

        IO.puts("closed")
        "x" = IO.binread(:stdio, 1)
    end
  end

  defp await_file(path, deadline) do
    case File.read(path) do
      {:ok, bytes} when bytes != "" ->
        :ok

      _ ->
        if System.monotonic_time(:millisecond) >= deadline, do: raise("Child did not start")
        Process.sleep(5)
        await_file(path, deadline)
    end
  end
end

[mode, path] = System.argv()
TinyAgent.BashFixture.run(mode, path)
