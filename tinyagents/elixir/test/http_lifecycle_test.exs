defmodule TinyAgent.HttpLifecycleTest do
  use ExUnit.Case, async: true
  import TinyAgent.HttpSupport
  alias TinyAgent.{CancellationToken, HttpServer, ProviderTurn, Session}
  @moduletag :tcp

  test "a cancellation body arriving after deletion cannot revive the removed session" do
    {_server, url} = server()
    id = create(url)
    socket = connect(url)

    :ok =
      :gen_tcp.send(
        socket,
        "POST /sessions/#{id}/cancel HTTP/1.1\r\nhost: fixture\r\ncontent-type: application/json\r\ncontent-length: 2\r\n\r\n{"
      )

    assert %{status: 204} = request(url, "DELETE", "/sessions/#{id}")
    :ok = :gen_tcp.send(socket, "}")
    assert %{status: 404} = response(socket)
  end

  test "the creating process exiting joins cooperative session cleanup" do
    observer = self()

    agent =
      TinyAgent.Agent.new(
        TinyAgent.CoreSupport.scripted(fn _, options ->
          send(observer, {:entered, self()})
          wait_until(fn -> CancellationToken.cancelled?(options[:cancellation]) end)
          send(observer, :cleaning)

          receive do
            :release -> :ok
          after
            5_000 -> flunk("Cleanup was not released")
          end

          CancellationToken.check!(options[:cancellation])
        end),
        "fixture"
      )

    parent =
      spawn(fn ->
        {:ok, server} = HttpServer.start_link(agent, port: 0)
        send(observer, {:server, server, URI.parse(HttpServer.url(server))})

        receive do
          :exit -> :ok
        end
      end)

    assert_receive {:server, server, url}, 2_000
    monitor = Process.monitor(server)
    id = create(url)
    socket = open(url, "POST", "/sessions/#{id}/prompts", %{"message" => "hold"})
    assert_receive {:entered, worker}, 2_000
    send(parent, :exit)
    assert_receive :cleaning, 2_000
    assert Process.alive?(server)
    refute_receive {:DOWN, ^monitor, :process, ^server, _}, 100
    send(worker, :release)
    assert_receive {:DOWN, ^monitor, :process, ^server, :normal}, 2_000
    assert List.last(events(response(socket)))["status"] == "aborted"
    refute Process.alive?(worker)
  end

  test "cancellation keeps a session busy through held cleanup and permits independent sessions" do
    owner = self()

    {server, url} =
      server(fn request, options ->
        if List.last(request.history).text == "hold" do
          send(owner, {:entered, self()})
          wait_until(fn -> CancellationToken.cancelled?(options[:cancellation]) end)
          send(owner, :cleaning)

          receive do
            :release -> :ok
          after
            5_000 -> flunk("Cleanup was not released")
          end

          CancellationToken.check!(options[:cancellation])
        end

        ProviderTurn.new("done", [], [], [])
      end)

    id = create(url)
    other = create(url)
    socket = open(url, "POST", "/sessions/#{id}/prompts", %{"message" => "hold"})
    assert_receive {:entered, worker}, 2_000

    assert %{status: 409} =
             request(url, "POST", "/sessions/#{id}/prompts", %{"message" => "busy"})

    assert %{status: 200} =
             request(url, "POST", "/sessions/#{other}/prompts", %{"message" => "independent"})

    assert %{status: 200, body: cancelled} = request(url, "POST", "/sessions/#{id}/cancel")
    assert %{"accepted" => true, "runId" => run_id} = Jason.decode!(cancelled)
    assert_receive :cleaning, 2_000
    reader = Task.async(fn -> response(socket) end)
    assert Task.yield(reader, 1_100) == nil

    assert %{status: 409} =
             request(url, "POST", "/sessions/#{id}/prompts", %{"message" => "still busy"})

    send(worker, :release)
    events = events(Task.await(reader, 2_000))
    assert List.last(events)["status"] == "aborted"
    assert List.last(events)["runId"] == run_id
    session = GenServer.call(server, {:session, id})
    assert Session.state(session).message_count == 0
  end

  test "deletion and simultaneous server shutdown retain provider cleanup" do
    owner = self()

    for shutdown <- [false, true] do
      {server, url} =
        server(fn _, options ->
          send(owner, {:entered, self()})
          wait_until(fn -> CancellationToken.cancelled?(options[:cancellation]) end)
          send(owner, :cleaning)

          receive do
            :release -> :ok
          after
            5_000 -> flunk("Cleanup was not released")
          end

          CancellationToken.check!(options[:cancellation])
        end)

      id = create(url)
      prompt = open(url, "POST", "/sessions/#{id}/prompts", %{"message" => "hold"})
      assert_receive {:entered, worker}, 2_000
      deletion = open(url, "DELETE", "/sessions/#{id}")
      assert_receive :cleaning, 2_000
      assert %{status: 404} = request(url, "POST", "/sessions/#{id}/cancel")
      closer = if shutdown, do: Task.async(fn -> HttpServer.close(server) end)
      if closer, do: assert(Task.yield(closer, 100) == nil)
      assert Process.alive?(worker)
      send(worker, :release)

      if closer do
        assert Task.await(closer, 2_000)
        assert {:error, :closed} = :gen_tcp.recv(deletion, 0, 2_000)
      else
        assert %{status: 204} = response(deletion)
      end

      assert List.last(events(response(prompt)))["status"] == "aborted"
      refute Process.alive?(worker)
      assert HttpServer.close(server)
    end
  end

  test "shutdown closes idle and pending body connections and callback close cannot self-wait" do
    owner = self()

    {server, url} =
      server(fn _, options ->
        send(owner, {:entered, self()})

        receive do
          {:server, server} -> send(owner, {:callback_closed, HttpServer.close(server)})
        after
          2_000 -> flunk("Server reference was not provided")
        end

        CancellationToken.check!(options[:cancellation])
      end)

    idle = connect(url)
    pending = connect(url)

    :ok =
      :gen_tcp.send(
        pending,
        "POST /sessions HTTP/1.1\r\nhost: fixture\r\ncontent-length: 10\r\ncontent-type: application/json\r\n\r\n{"
      )

    id = create(url)
    socket = open(url, "POST", "/sessions/#{id}/prompts", %{"message" => "stop"})
    assert_receive {:entered, worker}, 2_000
    send(worker, {:server, server})
    assert_receive {:callback_closed, false}, 2_000
    assert HttpServer.close(server)
    assert List.last(events(response(socket)))["status"] == "aborted"
    assert {:error, :closed} = :gen_tcp.recv(idle, 0, 2_000)
    assert {:error, :closed} = :gen_tcp.recv(pending, 0, 2_000)
  end

  test "cancelled pending native delta and final writes finish cleanup without committing" do
    owner = self()

    for phase <- [:delta, :terminal],
        termination <- [:cancel, :disconnect, :provider_cancel],
        phase == :delta or termination != :provider_cancel do
      {server, url} =
        server(fn request, options ->
          if List.last(request.history).text == "large" do
            send(owner, {:entered, self(), options[:cancellation]})
            text = String.duplicate("x", 900_000)

            try do
              if phase == :delta do
                for _ <- 1..30, do: options[:on_text].(text, options[:cancellation])
                flunk("Restricted reader did not block output")
              else
                ProviderTurn.new(text, [], [], [])
              end
            after
              send(owner, :provider_finished)
            end
          else
            send(owner, {:history, length(request.history)})
            ProviderTurn.new("done", [], [], [])
          end
        end)

      id = create(url)
      session = GenServer.call(server, {:session, id})

      socket =
        open(url, "POST", "/sessions/#{id}/prompts", %{"message" => "large"}, [], recbuf: 4096)

      assert_receive {:entered, worker, token}, 2_000
      if phase == :terminal, do: assert_receive(:provider_finished, 2_000)
      Process.sleep(100)
      assert Session.state(session).run_id != nil
      assert Session.state(session).message_count == 0

      case termination do
        :disconnect ->
          :gen_tcp.close(socket)

        :provider_cancel ->
          CancellationToken.cancel(token)

        :cancel ->
          assert %{status: 200, body: body} = request(url, "POST", "/sessions/#{id}/cancel")
          assert Jason.decode!(body)["accepted"]
      end

      wait_until(fn -> Session.state(session).run_id == nil end, 300)
      refute Process.alive?(worker)
      if phase == :delta, do: assert_receive(:provider_finished, 2_000)
      assert Session.state(session).message_count == 0
      :gen_tcp.close(socket)

      assert %{status: 200} =
               request(url, "POST", "/sessions/#{id}/prompts", %{"message" => "after"})

      assert_receive {:history, 1}
      assert HttpServer.close(server)
    end
  end
end
