defmodule TinyAgent.NativeCLIProvider do
  defstruct [:mode, :path]
  alias TinyAgent.{CancellationToken, ProviderTurn, ToolCall}

  def stream(provider, request, options) do
    IO.puts(:stderr, "request:#{length(request.history)}")
    prompt = List.last(request.history).text

    try do
      case provider.mode do
        mode when mode in ["conversation", "rollback"] ->
          options[:on_text].("done", options[:cancellation])
          if prompt == "fail", do: raise("fixture failed")
          ProviderTurn.new("done", [], [], [])

        "request" ->
          wait(options[:cancellation])

        "bash" ->
          path = String.replace(provider.path, "'", "'\\''")
          command = "sleep 30 & child=$!; printf '%s %s' \"$$\" \"$child\" > '#{path}'; wait"
          ProviderTurn.new("", [ToolCall.new("bash", "bash", %{"command" => command})], [], [])

        mode when mode in ["stalled", "broken"] ->
          options[:on_text].(String.duplicate("x", 262_144), options[:cancellation])
          raise "Stalled output unexpectedly completed"

        "idle" ->
          raise "Idle CLI entered provider"
      end
    after
      IO.puts(:stderr, "provider_closed")
    end
  end

  defp wait(token) do
    CancellationToken.check!(token)
    Process.sleep(5)
    wait(token)
  end
end

[mode, path] = System.argv()

result =
  try do
    TinyAgent.CLI.run(struct!(TinyAgent.NativeCLIProvider, mode: mode, path: path), "fixture")
    :ok
  rescue
    error ->
      IO.puts(:stderr, "cli_error:#{Exception.message(error)}")
      :error
  end

{:ok, signal} = TinyAgent.RpcIO.watch_interrupt()
false = TinyAgent.RpcIO.interrupted?(signal)
:ok = TinyAgent.RpcIO.close(signal)
IO.puts(:stderr, "restored")
unless mode in ["conversation", "rollback"], do: "q" = IO.binread(:stdio, 1)
System.halt(if result == :ok, do: 0, else: 1)
