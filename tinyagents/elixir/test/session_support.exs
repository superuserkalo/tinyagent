defmodule TinyAgent.SessionSupport do
  import ExUnit.Callbacks
  alias TinyAgent.{Agent, CancellationToken, ProviderTurn, RpcSession}

  def session(provider, emit \\ nil) do
    owner = self()
    emit = emit || fn value, token -> send(owner, {:rpc, value, token}) end
    rpc = RpcSession.new(Agent.new(provider, "fixture"), emit)
    on_exit(fn -> RpcSession.close(rpc) end)
    rpc
  end

  def text_provider do
    TinyAgent.CoreSupport.scripted(fn _request, options ->
      options[:on_text].("done", options[:cancellation])
      ProviderTurn.new("done", [], [], [])
    end)
  end

  def prompt(rpc, text, id \\ "prompt"),
    do: RpcSession.handle(rpc, %{"id" => id, "type" => "prompt", "message" => text})

  def messages do
    receive do
      {:rpc, value, _token} -> [value | messages()]
    after
      0 -> []
    end
  end

  def until_cancelled(token),
    do: until_cancelled(token, System.monotonic_time(:millisecond) + 1_000)

  defp until_cancelled(token, deadline) do
    cond do
      CancellationToken.cancelled?(token) ->
        CancellationToken.check!(token)

      System.monotonic_time(:millisecond) >= deadline ->
        raise "Cancellation was not received"

      true ->
        receive do
        after
          1 -> until_cancelled(token, deadline)
        end
    end
  end
end
