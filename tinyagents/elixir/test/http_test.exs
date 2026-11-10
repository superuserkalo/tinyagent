defmodule TinyAgent.HttpTest do
  use ExUnit.Case, async: true
  import TinyAgent.HttpSupport
  alias TinyAgent.{CancellationToken, HttpServer, ProviderTurn}
  @moduletag :tcp
  @moduletag capture_log: true

  test "IPv4 and IPv6 require explicit authenticated remote exposure" do
    for host <- ["127.0.0.1", "::1"] do
      {server, url} =
        server(fn _, _ -> ProviderTurn.new("", [], [], []) end, host: host, token: "secret")

      for headers <- [
            [],
            [{"authorization", "Bearer wrong"}],
            [{"authorization", "Bearer secret"}, {"authorization", "Bearer secret"}]
          ] do
        assert %{status: 401} = request(url, "GET", "/health", "", headers)
      end

      assert %{status: 200} =
               request(url, "GET", "/health", "", [{"authorization", "Bearer secret"}])

      assert HttpServer.close(server)
    end

    agent =
      TinyAgent.Agent.new(
        TinyAgent.CoreSupport.scripted(fn _, _ -> ProviderTurn.new("", [], [], []) end),
        "fixture"
      )

    for options <- [
          [host: "0.0.0.0"],
          [host: "0.0.0.0", token: "secret"],
          [host: "::", allow_remote: true],
          [token: ""],
          [token: "bad\n"],
          [port: -1]
        ] do
      assert_raise ArgumentError, fn -> HttpServer.start_link(agent, options) end
    end

    {:ok, server} =
      HttpServer.start_link(agent, host: "0.0.0.0", port: 0, token: "secret", allow_remote: true)

    url = %{URI.parse(HttpServer.url(server)) | host: "127.0.0.1"}

    assert %{status: 200} =
             request(url, "GET", "/health", "", [{"authorization", "Bearer secret"}])

    assert HttpServer.close(server)
  end

  test "JSON bodies, media types and byte limits fail before a run starts" do
    {_server, url} = server()
    id = create(url)
    path = "/sessions/#{id}/prompts"

    for {body, headers, status} <- [
          {"{}", [], 415},
          {"{}", [{"content-type", "text/plain"}], 415},
          {"{}", [{"content-type", "application/json"}, {"content-type", "application/json"}],
           415},
          {"{", [{"content-type", "application/json"}], 400},
          {<<255>>, [{"content-type", "application/json"}], 400},
          {"[]", [{"content-type", "application/json"}], 400},
          {%{}, [], 400},
          {%{"message" => ""}, [], 400},
          {%{"message" => 42}, [], 400}
        ] do
      assert %{status: ^status} = request(url, "POST", path, body, headers)
    end

    socket = connect(url)

    :ok =
      :gen_tcp.send(
        socket,
        "POST #{path} HTTP/1.1\r\nhost: fixture\r\ncontent-length: 1048577\r\n\r\n"
      )

    assert %{status: 413} = response(socket)
    payload = Jason.encode!(%{"message" => "ok", "pad" => ""})

    exact =
      Jason.encode!(%{
        "message" => "ok",
        "pad" => String.duplicate("x", 1_048_576 - byte_size(payload))
      })

    assert byte_size(exact) == 1_048_576

    assert %{status: 200} =
             request(url, "POST", path, exact, [{"content-type", "application/json"}])

    assert %{status: 413} =
             request(url, "POST", path, exact <> " ", [{"content-type", "application/json"}])
  end

  test "native framing accepts fragmented chunked UTF-8 and rejects malformed lengths" do
    {_server, url} = server()
    id = create(url)
    path = "/sessions/#{id}/prompts"
    socket = connect(url)

    :ok =
      :gen_tcp.send(
        socket,
        "POST #{path} HTTP/1.1\r\nhost: fixture\r\ncontent-type: application/json\r\ntransfer-encoding: chunked\r\n\r\n"
      )

    for byte <- :binary.bin_to_list(Jason.encode!(%{"message" => "café"})) do
      :ok = :gen_tcp.send(socket, ["1\r\n", <<byte>>, "\r\n"])
    end

    :ok = :gen_tcp.send(socket, "0\r\n\r\nGET /health HTTP/1.1\r\nhost: fixture\r\n\r\n")
    assert %{status: 200} = response(socket)

    for headers <- [
          "content-length: nope",
          "content-length: 1\r\ncontent-length: 2",
          "content-length: 1\r\ntransfer-encoding: chunked"
        ] do
      socket = connect(url)

      :ok =
        :gen_tcp.send(socket, "POST /sessions HTTP/1.1\r\nhost: fixture\r\n#{headers}\r\n\r\n")

      assert %{status: 400} = response(socket)
    end
  end

  test "provider failures and oversized delta or terminal events roll history back" do
    owner = self()

    {_server, url} =
      server(fn request, options ->
        send(owner, {:history, length(request.history)})

        case List.last(request.history).text do
          "fail" -> raise "provider failed"
          "delta" -> options[:on_text].(String.duplicate("x", 1_048_577), options[:cancellation])
          "terminal" -> ProviderTurn.new(String.duplicate("x", 1_048_577), [], [], [])
          _ -> ProviderTurn.new("done", [], [], [])
        end
      end)

    id = create(url)
    path = "/sessions/#{id}/prompts"

    assert List.last(events(request(url, "POST", path, %{"message" => "first"})))["status"] ==
             "completed"

    assert_receive {:history, 1}

    for prompt <- ["fail", "delta", "terminal"] do
      result = events(request(url, "POST", path, %{"message" => prompt}))
      assert List.last(result)["status"] == "failed"

      assert List.last(result)["error"]["code"] ==
               if(prompt == "fail", do: "provider_error", else: "output_too_large")

      assert Enum.all?(result, &(byte_size(Jason.encode!(&1)) <= 1_048_576))
      assert Enum.map(result, & &1["sequence"]) == [1, 2]
      assert_receive {:history, 3}
    end

    assert List.last(events(request(url, "POST", path, %{"message" => "after"})))["status"] ==
             "completed"

    assert_receive {:history, 3}
  end

  test "native HTTP routes stream ordered events and retain only completed history" do
    owner = self()

    {server, url} =
      server(fn request, options ->
        send(owner, {:history, request.history})
        options[:on_text].("café\n", options[:cancellation])
        ProviderTurn.new("answer", [], [], [])
      end)

    assert url.host == "127.0.0.1"
    assert %{status: 200, body: body} = request(url, "GET", "/health")
    assert Jason.decode!(body) == %{"status" => "ok", "protocolVersion" => 1}
    id = create(url)
    path = "/sessions/#{id}"

    for {prompt, count} <- [{"first", 1}, {"second", 3}] do
      response = request(url, "POST", path <> "/prompts", %{"message" => prompt})
      assert {"content-type", "application/jsonl; charset=utf-8"} in response.headers

      assert Enum.all?(response.headers, fn {name, _} ->
               not String.starts_with?(name, "access-control-")
             end)

      events = events(response)
      assert Enum.map(events, & &1["type"]) == ["agent_start", "text_delta", "agent_end"]
      assert Enum.map(events, & &1["sequence"]) == [1, 2, 3]
      assert length(Enum.uniq(Enum.map(events, & &1["runId"]))) == 1
      assert List.last(events)["status"] == "completed"
      assert_receive {:history, history}
      assert length(history) == count
    end

    # A client may close as soon as HTTP's final chunk arrives, without waiting
    # for server EOF. Completed delivery must still commit its history.
    session = GenServer.call(server, {:session, id})

    for index <- 3..12 do
      socket = open(url, "POST", path <> "/prompts", %{"message" => "close after terminal"})
      close_after_terminal(socket, "")
      wait_until(fn -> TinyAgent.Session.state(session).run_id == nil end)
      assert TinyAgent.Session.state(session).message_count == index * 2
      assert_receive {:history, history}
      assert length(history) == index * 2 - 1
    end

    assert %{status: 200, body: body} = request(url, "POST", path <> "/cancel")
    assert Jason.decode!(body) == %{"accepted" => false, "runId" => nil}
    assert %{status: 204, body: ""} = request(url, "DELETE", path)
    assert %{status: 404} = request(url, "POST", path <> "/prompts", %{"message" => "gone"})
    assert HttpServer.close(server)
    assert HttpServer.close(server)
  end

  test "silent disconnect cancels the provider and preserves prior history" do
    owner = self()

    {_server, url} =
      server(fn request, options ->
        send(owner, {:entered, self(), request.history})

        if List.last(request.history).text == "silent" do
          wait_until(fn -> CancellationToken.cancelled?(options[:cancellation]) end)
          send(owner, :cancelled)
          CancellationToken.check!(options[:cancellation])
        end

        ProviderTurn.new("done", [], [], [])
      end)

    id = create(url)
    path = "/sessions/#{id}/prompts"
    socket = open(url, "POST", path, %{"message" => "silent"})
    assert_receive {:entered, _worker, [_]}, 2_000
    :gen_tcp.close(socket)
    assert_receive :cancelled, 2_000

    wait_until(fn ->
      request(url, "POST", "/sessions/#{id}/cancel").body
      |> Jason.decode!()
      |> Map.fetch!("accepted") == false
    end)

    assert %{status: 200} = request(url, "POST", path, %{"message" => "after"})
    assert_receive {:entered, _, [_]}, 2_000
  end

  defp close_after_terminal(socket, bytes) do
    {:ok, more} = :gen_tcp.recv(socket, 0, 2_000)
    bytes = bytes <> more

    if String.ends_with?(bytes, "0\r\n\r\n"),
      do: :gen_tcp.close(socket),
      else: close_after_terminal(socket, bytes)
  end
end
