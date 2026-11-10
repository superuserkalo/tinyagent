defmodule TinyAgent.RpcApiProvider do
  @behaviour TinyAgent.Provider
  defstruct [:owner]

  @impl true
  def stream(provider, _request, options) do
    try do
      options[:on_text].("waiting", options[:cancellation])
      send(provider.owner, {:provider, self()})
      wait(options[:cancellation])
    after
      IO.puts(:stderr, "provider cleaned")
    end
  end

  defp wait(token) do
    TinyAgent.CancellationToken.check!(token)
    Process.sleep(1)
    wait(token)
  end
end

defmodule TinyAgent.RpcApiFixture do
  import ExUnit.Assertions
  alias TinyAgent.{Agent, CancellationToken, Cancelled, Rpc, RpcApiProvider, RpcError, RpcIO}

  def run([mode | descriptors]) do
    Application.ensure_all_started(:crypto)
    agent = Agent.new(%RpcApiProvider{owner: self()}, "fixture")

    case mode do
      "invalid" ->
        invalid(agent)

      "external_cancel" ->
        [descriptor] = descriptors
        token = CancellationToken.new()

        {control, monitor} =
          spawn_monitor(fn ->
            release(String.to_integer(descriptor))
            CancellationToken.cancel(token)
          end)

        assert_raise Cancelled, fn -> Rpc.run(agent, input: 0, output: 1, cancellation: token) end
        receive do: ({:DOWN, ^monitor, :process, ^control, :normal} -> :ok)
        receive do: ({:provider, _} -> :ok)
        IO.puts(:stderr, "cancelled")

      "owner_exit" ->
        [descriptor] = descriptors
        {owner, owner_monitor} = spawn_monitor(fn -> Rpc.run_stdio(agent) end)
        worker = receive do: ({:provider, worker} -> worker)
        {:links, links} = Process.info(owner, :links)
        {:monitors, monitors} = Process.info(owner, :monitors)
        children = links ++ Enum.map(monitors, fn {:process, pid} -> pid end) ++ [worker]
        assert length(children) == 4
        watchers = Enum.map(children, &{&1, Process.monitor(&1)})
        IO.puts(:stderr, "owner ready")
        release(String.to_integer(descriptor))
        Process.exit(owner, :shutdown)
        receive do: ({:DOWN, ^owner_monitor, :process, ^owner, :shutdown} -> :ok)

        for {child, monitor} <- watchers do
          receive do
            {:DOWN, ^monitor, :process, ^child, _reason} -> :ok
          after
            3_000 -> flunk("RPC child outlived its owner: #{inspect(child)}")
          end
        end

        {:ok, signal} = RpcIO.watch_interrupt()
        :ok = RpcIO.close(signal)
        IO.puts(:stderr, "owner cleaned")
    end

    assert Process.info(self(), :messages) == {:messages, []}
  end

  defp invalid(agent) do
    {:links, before_links} = Process.info(self(), :links)

    for options <- [
          [input: -1, output: 1],
          [input: 0, output: 0],
          [input: "0", output: 1],
          [input: 0, output: 1, handle_interrupt: :yes],
          [input: 0, output: 1, unknown: true]
        ] do
      assert_raise ArgumentError, fn -> Rpc.run(agent, options) end
    end

    assert_raise KeyError, fn -> Rpc.run(agent, input: 0) end
    assert_raise ArgumentError, fn -> Rpc.run_stdio(agent, input: 0) end
    token = CancellationToken.new()
    CancellationToken.cancel(token)

    assert_raise Cancelled, fn ->
      Rpc.run(agent, input: 0, output: 2_000_000_000, cancellation: token)
    end

    {:ok, signal} = RpcIO.watch_interrupt()
    error = assert_raise RpcError, fn -> Rpc.run_stdio(agent) end
    assert error.code == "signal_error"
    :ok = RpcIO.close(signal)

    for {options, code} <- [
          {[input: 0, output: 2_000_000_000, handle_interrupt: true], "output_error"},
          {[input: 1, output: 0], "output_error"},
          {[input: 1, output: 2], "input_error"}
        ] do
      error = assert_raise RpcError, fn -> Rpc.run(agent, options) end
      assert error.code == code
      {:ok, signal} = RpcIO.watch_interrupt()
      :ok = RpcIO.close(signal)
    end

    {:links, after_links} = Process.info(self(), :links)
    assert MapSet.new(before_links) == MapSet.new(after_links)
    IO.puts(:stderr, "invalid checked")
  end

  defp release(descriptor) do
    {:ok, handle} = RpcIO.open(descriptor, 0)

    try do
      await_byte(handle)
    after
      RpcIO.close(handle)
    end
  end

  defp await_byte(handle) do
    case RpcIO.read(handle) do
      {:ok, "x"} ->
        :ok

      :again ->
        Process.sleep(1)
        await_byte(handle)
    end
  end
end

TinyAgent.RpcApiFixture.run(System.argv())
