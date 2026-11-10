defmodule TinyAgent.RpcFixtureProvider do
  @behaviour TinyAgent.Provider
  defstruct [:control]
  alias TinyAgent.{CancellationToken, ProviderError, ProviderTurn, RpcIO}

  @impl true
  def stream(provider, request, options) do
    prompt = List.last(request.history).text
    IO.puts(:stderr, "provider:#{prompt}:#{length(request.history)}")

    try do
      case prompt do
        "wait" ->
          options[:on_text].("waiting", options[:cancellation])
          wait(options[:cancellation])

        "held" ->
          options[:on_text].("waiting", options[:cancellation])
          wait(options[:cancellation])

        "huge" ->
          options[:on_text].(String.duplicate("x", 1_048_576), options[:cancellation])

        "delta" ->
          options[:on_text].(String.duplicate("x", 262_144), options[:cancellation])

        "error" ->
          options[:on_text].("partial", options[:cancellation])
          raise ProviderError, message: "fixture failure"

        "terminal" ->
          options[:on_text].("preparing", options[:cancellation])

        _ ->
          options[:on_text].("done\ncafé", options[:cancellation])
      end

      text = if prompt == "terminal", do: String.duplicate("x", 262_144), else: "done\ncafé"
      ProviderTurn.new(text, [], [], [])
    after
      IO.puts(:stderr, "cleanup:#{prompt}")

      if prompt == "held" do
        {:ok, handle} = RpcIO.open(provider.control, 0)

        try do
          release(handle)
        after
          RpcIO.close(handle)
        end
      end
    end
  end

  defp wait(token) do
    CancellationToken.check!(token)
    Process.sleep(1)
    wait(token)
  end

  defp release(handle) do
    case RpcIO.read(handle) do
      :again ->
        Process.sleep(1)
        release(handle)

      {:ok, "x"} ->
        :ok
    end
  end
end

defmodule TinyAgent.RpcFixture do
  alias TinyAgent.{Agent, Rpc, RpcError, RpcFixtureProvider}

  def run do
    Application.ensure_all_started(:crypto)

    control =
      case System.argv() do
        [] -> nil
        [descriptor] -> String.to_integer(descriptor)
      end

    agent = Agent.new(%RpcFixtureProvider{control: control}, "fixture")
    {:links, links} = Process.info(self(), :links)
    send(self(), :caller_message)

    result =
      try do
        Rpc.run_stdio(agent)
      rescue
        error in RpcError -> {:error, error.code}
      end

    receive do: (:caller_message -> :ok)
    {:links, remaining} = Process.info(self(), :links)
    if MapSet.new(remaining) != MapSet.new(links), do: raise("RPC left linked processes")
    if Process.info(self(), :messages) != {:messages, []}, do: raise("RPC left caller messages")

    case result do
      :ok ->
        :ok

      {:error, code} ->
        IO.puts(:stderr, "error:#{code}")
        System.halt(2)
    end
  end
end

TinyAgent.RpcFixture.run()
