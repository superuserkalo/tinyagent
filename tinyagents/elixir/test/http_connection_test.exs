defmodule TinyAgent.HttpConnectionTest do
  use ExUnit.Case, async: true
  import TinyAgent.CoreSupport
  alias TinyAgent.{Agent, HttpConnection, HttpTransport, ProviderTurn, Session}
  @moduletag :tcp

  defmodule PausedFinalAdapter do
    def send_chunked(socket, 200, _headers), do: {:ok, nil, socket}

    def chunk(socket, "") do
      :ok = :gen_tcp.send(socket, "0\r\n\r\n")

      # Keep the successful write's return pending until the disconnect watcher
      # has observed client EOF and closed its socket. This forces the race.
      TinyAgent.HttpSupport.wait_until(fn -> match?({:error, _}, :inet.sockname(socket)) end)
      {:ok, nil, socket}
    end

    def chunk(socket, bytes) do
      :ok = :gen_tcp.send(socket, bytes)
      {:ok, nil, socket}
    end
  end

  test "client EOF before final write acknowledgement preserves completed history" do
    {:ok, listener} =
      :gen_tcp.listen(0, [:binary, active: false, ip: {127, 0, 0, 1}])

    on_exit(fn -> :gen_tcp.close(listener) end)
    {:ok, {ip, port}} = :inet.sockname(listener)
    provider = scripted(fn _, _ -> ProviderTurn.new("answer", [], [], []) end)
    session = start_supervised!({Session, Agent.new(provider, "fixture")})

    connection =
      Task.async(fn ->
        {:ok, socket} = :gen_tcp.accept(listener, 2_000)
        Process.put(HttpTransport, socket)

        try do
          HttpConnection.stream(%Plug.Conn{adapter: {PausedFinalAdapter, socket}}, session, "go")
        after
          :gen_tcp.close(socket)
        end
      end)

    {:ok, client} = :gen_tcp.connect(ip, port, [:binary, active: false], 2_000)

    try do
      bytes = terminal(client, "")
      :ok = :gen_tcp.close(client)
      assert %Plug.Conn{state: :sent} = Task.await(connection, 3_000)
      assert bytes =~ ~s("status":"completed")
      assert Session.state(session) == %{run_id: nil, message_count: 2}
    after
      :gen_tcp.close(client)
      Task.shutdown(connection, :brutal_kill)
    end
  end

  defp terminal(socket, bytes) do
    if String.ends_with?(bytes, "0\r\n\r\n") do
      bytes
    else
      {:ok, more} = :gen_tcp.recv(socket, 0, 2_000)
      terminal(socket, bytes <> more)
    end
  end
end
