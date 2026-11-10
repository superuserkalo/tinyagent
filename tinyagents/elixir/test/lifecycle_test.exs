defmodule TinyAgent.LifecycleTest do
  use ExUnit.Case, async: true
  import TinyAgent.TestSupport

  alias TinyAgent.{
    AnthropicProvider,
    CancellationToken,
    Cancelled,
    FixtureTransport,
    OpenAIProvider,
    ProviderError
  }

  test "completion closes a response without waiting for HTTP EOF" do
    for {module, prefix} <- providers() do
      {provider, socket} =
        provider(module, fixture(prefix <> "_text"), framing: :open, eof: false)

      assert stream(provider, request()).text == "done\ncafé"
      assert FixtureTransport.snapshot(socket).closed
    end
  end

  test "cancellation interrupts header and body waits and preserves caller messages" do
    for {module, _prefix} <- providers(), phase <- [:headers, :body] do
      fragments =
        if phase == :headers,
          do: [],
          else: ["HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n"]

      {provider, socket} = provider(module, "", fragments: fragments, eof: false)
      token = CancellationToken.new()

      worker =
        Task.async(fn ->
          foreign = make_ref()
          send(self(), {:tcp, foreign, "unrelated"})
          send(self(), :caller_message)
          result = capture(fn -> stream(provider, request(), cancellation: token) end)
          assert_receive {:tcp, ^foreign, "unrelated"}
          assert_receive :caller_message
          refute_receive {:tcp, ^socket, _}, 0
          refute_receive {:tcp_closed, ^socket}, 0
          result
        end)

      assert_receive {:waiting, ^socket}, 1_000
      CancellationToken.cancel(token)
      assert %Cancelled{} = Task.await(worker, 1_000)
      assert FixtureTransport.snapshot(socket).closed
    end
  end

  test "deadline interrupts header and body waits without cancelling the parent" do
    for {module, _prefix} <- providers(), phase <- [:headers, :body] do
      fragments =
        if phase == :headers,
          do: [],
          else: ["HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n"]

      {provider, socket} = provider(module, "", fragments: fragments, eof: false, timeout: 30)
      token = CancellationToken.new()

      assert_raise ProviderError, "Provider request timed out.", fn ->
        stream(provider, request(), cancellation: token)
      end

      refute CancellationToken.cancelled?(token)
      assert FixtureTransport.snapshot(socket).closed
    end
  end

  test "callback cancellation and failure close the response before returning" do
    for {module, prefix} <- providers() do
      {provider, socket} = provider(module, fixture(prefix <> "_text"), fragment_size: 8192)
      token = CancellationToken.new()

      assert_raise Cancelled, fn ->
        stream(provider, request(),
          cancellation: token,
          on_text: fn _text, scope ->
            CancellationToken.cancel(scope)
          end
        )
      end

      refute CancellationToken.cancelled?(token)
      assert FixtureTransport.snapshot(socket).closed
      {provider, socket} = provider(module, fixture(prefix <> "_text"), fragment_size: 8192)

      error =
        assert_raise ProviderError, fn ->
          stream(provider, request(), on_text: fn _text, _scope -> raise "consumer failure" end)
        end

      assert %RuntimeError{message: "consumer failure"} = error.cause
      assert FixtureTransport.snapshot(socket).closed
    end
  end

  test "callback remains in the caller process and its deadline wins over a later exception" do
    for {module, prefix} <- providers() do
      {provider, socket} =
        provider(module, fixture(prefix <> "_text"), timeout: 30, fragment_size: 8192)

      caller = self()

      assert_raise ProviderError, "Provider request timed out.", fn ->
        stream(provider, request(),
          on_text: fn _text, scope ->
            assert self() == caller
            Process.sleep(40)
            assert CancellationToken.cancelled?(scope)
            raise "late failure"
          end
        )
      end

      assert FixtureTransport.snapshot(socket).closed
    end
  end

  test "cancellation while cleanup is held prevents a successful return" do
    observer = self()

    for {module, prefix} <- providers() do
      close = fn ->
        send(observer, {:closing, self()})

        receive do
          :release_cleanup -> :ok
        after
          1_000 -> raise "Cleanup release was not received."
        end
      end

      {provider, socket} =
        provider(module, fixture(prefix <> "_text"), close: close, fragment_size: 8192)

      token = CancellationToken.new()

      worker =
        Task.async(fn -> capture(fn -> stream(provider, request(), cancellation: token) end) end)

      assert_receive {:closing, owner}, 1_000
      assert owner == worker.pid
      assert Task.yield(worker, 0) == nil
      CancellationToken.cancel(token)
      send(owner, :release_cleanup)
      assert %Cancelled{} = Task.await(worker, 1_000)
      assert FixtureTransport.snapshot(socket).closed
    end
  end

  test "cleanup deadline and queued owned socket messages are settled before return" do
    for {module, prefix} <- providers() do
      {provider, socket} =
        provider(module, fixture(prefix <> "_text"), timeout: 30, fragment_size: 8192)

      Agent.update(socket, fn state ->
        %{
          state
          | close: fn ->
              send(self(), {:tcp, socket, "leftover"})
              send(self(), {:tcp_closed, socket})
              Process.sleep(40)
            end
        }
      end)

      assert_raise ProviderError, "Provider request timed out.", fn ->
        stream(provider, request())
      end

      refute_receive {:tcp, ^socket, _}, 0
      refute_receive {:tcp_closed, ^socket}, 0
      assert FixtureTransport.snapshot(socket).closed
    end
  end

  test "pre-cancelled calls do not connect or require an API key" do
    token = CancellationToken.new()
    CancellationToken.cancel(token)

    for {module, _prefix} <- providers() do
      provider = module.new(connect: fn _, _, _, _ -> flunk("Unexpected connection") end)
      assert_raise Cancelled, fn -> stream(provider, request(), cancellation: token) end
    end
  end

  defp providers, do: [{OpenAIProvider, "openai"}, {AnthropicProvider, "anthropic"}]

  defp capture(callback) do
    callback.()
  rescue
    error -> error
  end
end
