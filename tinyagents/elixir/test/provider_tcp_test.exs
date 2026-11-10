defmodule TinyAgent.ProviderTCPTest do
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

  @moduletag :tcp

  test "native TCP completion closes a body whose HTTP terminator is withheld" do
    for {module, prefix} <- providers() do
      {listener, port} = listen!()

      body =
        FixtureTransport.response(fixture(prefix <> "_text"), framing: :open, fragment_size: 8192)

      server = serve(listener, body, self())

      provider =
        module.new(
          api_key: "fixture-key",
          endpoint: "http://127.0.0.1:#{port}/stream",
          timeout: 2_000
        )

      assert stream(provider, request()).text == "done\ncafé"
      assert Task.await(server, 3_000) == :closed
    end
  end

  test "native TCP cancellation and deadlines close header and body waits" do
    for {module, _prefix} <- providers(),
        phase <- [:headers, :body],
        reason <- [:cancel, :timeout] do
      {listener, port} = listen!()

      body =
        if phase == :headers,
          do: [],
          else: ["HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n"]

      server = serve(listener, body, self())
      timeout = if reason == :timeout, do: 300, else: 2_000

      provider =
        module.new(
          api_key: "fixture-key",
          endpoint: "http://127.0.0.1:#{port}/stream",
          timeout: timeout
        )

      token = CancellationToken.new()

      worker =
        Task.async(fn ->
          try do
            stream(provider, request(), cancellation: token)
          rescue
            error -> error
          end
        end)

      owner = server.pid
      assert_receive {:received, ^owner}, 1_000
      if reason == :cancel, do: CancellationToken.cancel(token)
      error = Task.await(worker, 2_000)

      if reason == :cancel,
        do: assert(%Cancelled{} = error),
        else: assert(%ProviderError{message: "Provider request timed out."} = error)

      assert Task.await(server, 3_000) == :closed
    end
  end

  defp listen! do
    case :gen_tcp.listen(0, [:binary, active: false, ip: {127, 0, 0, 1}, reuseaddr: true]) do
      {:ok, listener} ->
        on_exit(fn -> :gen_tcp.close(listener) end)
        {:ok, {_, port}} = :inet.sockname(listener)
        {listener, port}

      {:error, reason} ->
        flunk("Mandatory native TCP listener failed: #{inspect(reason)}")
    end
  end

  defp serve(listener, body, observer) do
    Task.async(fn ->
      {:ok, socket} = :gen_tcp.accept(listener, 2_000)

      try do
        read_request(socket, "")
        Enum.each(body, fn bytes -> :ok = :gen_tcp.send(socket, bytes) end)
        send(observer, {:received, self()})
        assert {:error, :closed} = :gen_tcp.recv(socket, 0, 2_000)
        :closed
      after
        :gen_tcp.close(socket)
        :gen_tcp.close(listener)
      end
    end)
  end

  defp read_request(socket, bytes) do
    complete =
      case String.split(bytes, "\r\n\r\n", parts: 2) do
        [headers, body] ->
          [_, length] = Regex.run(~r/content-length: (\d+)/i, headers)
          byte_size(body) >= String.to_integer(length)

        _ ->
          false
      end

    unless complete do
      assert byte_size(bytes) < 1_048_576
      {:ok, more} = :gen_tcp.recv(socket, 0, 2_000)
      read_request(socket, bytes <> more)
    end
  end

  defp providers, do: [{OpenAIProvider, "openai"}, {AnthropicProvider, "anthropic"}]
end
