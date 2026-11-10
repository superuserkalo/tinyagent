defmodule TinyAgent.SessionLifecycleTest do
  use ExUnit.Case, async: true
  import TinyAgent.CoreSupport
  import TinyAgent.SessionSupport

  alias TinyAgent.{
    Agent,
    AnthropicProvider,
    CancellationToken,
    OpenAIProvider,
    ProviderTurn,
    RpcSession
  }

  test "reset clears history immediately and waits for cooperative cleanup" do
    owner = self()

    provider =
      scripted(fn request, options ->
        text = List.last(request.history).text

        if text == "wait" do
          send(owner, :provider_entered)

          try do
            until_cancelled(options[:cancellation])
          after
            send(owner, {:cleanup, self()})

            receive do
              :release -> :ok
            after
              1_000 -> flunk("Cleanup was not released")
            end
          end
        end

        ProviderTurn.new(text, [], [], [])
      end)

    rpc = session(provider)
    prompt(rpc, "seed")
    RpcSession.wait_idle(rpc)
    assert RpcSession.get_state(rpc)["messageCount"] == 2
    messages()
    prompt(rpc, "wait")
    assert_receive :provider_entered, 1_000

    resetting =
      Task.async(fn -> RpcSession.handle(rpc, %{"id" => "reset", "type" => "new_session"}) end)

    assert_receive {:cleanup, worker}, 1_000
    assert Task.yield(resetting, 0) == nil
    assert RpcSession.get_state(rpc)["messageCount"] == 0
    assert RpcSession.get_state(rpc)["status"] == "running"
    prompt(rpc, "busy", "busy")
    assert_receive {:rpc, %{"id" => "busy", "error" => %{"code" => "busy"}}, nil}
    RpcSession.handle(rpc, %{"id" => "second reset", "type" => "new_session"})
    assert_receive {:rpc, %{"id" => "second reset", "error" => %{"code" => "busy"}}, nil}
    send(worker, :release)
    assert Task.await(resetting, 1_000) == :ok
    assert_receive {:rpc, %{"id" => "reset", "data" => %{"abortedRun" => true}}, nil}
    assert List.last(messages())["status"] == "aborted"
    assert RpcSession.get_state(rpc)["status"] == "idle"
    prompt(rpc, "new")
    RpcSession.wait_idle(rpc)
    assert RpcSession.get_state(rpc)["messageCount"] == 2
  end

  test "close cancels held terminal delivery and joins the worker and controller" do
    owner = self()

    rpc =
      session(text_provider(), fn value, token ->
        send(owner, {:rpc, value, token})

        if value["status"] == "completed" do
          send(owner, {:delivering, self(), token})

          receive do
            :release -> :ok
          after
            1_000 -> flunk("Delivery was not released")
          end
        end
      end)

    prompt(rpc, "go")
    assert_receive {:delivering, worker, token}, 1_000
    closing = Task.async(fn -> RpcSession.close(rpc) end)
    assert_raise TinyAgent.Cancelled, fn -> until_cancelled(token) end
    prompt(rpc, "closed", "after close")
    assert_receive {:rpc, %{"id" => "after close", "error" => %{"code" => "session_closed"}}, nil}
    assert Task.yield(closing, 0) == nil
    assert CancellationToken.cancelled?(token)
    assert RpcSession.get_state(rpc)["messageCount"] == 0
    send(worker, :release)
    assert Task.await(closing, 1_000)
    refute Process.alive?(worker)
    refute Process.alive?(rpc.session)
    assert Enum.count(messages(), &(&1["type"] == "agent_end")) == 1
  end

  test "reset and close from run callbacks do not wait on themselves" do
    owner = self()

    for action <- [:reset, :close], phase <- [:ack, :delta, :completed] do
      holder = start_supervised!({Elixir.Agent, fn -> nil end}, id: make_ref())

      callback = fn value, token ->
        send(owner, {:rpc, value, token})

        selected =
          case phase do
            :ack -> value["command"] == "prompt" and value["success"] == true
            :delta -> value["type"] == "text_delta"
            :completed -> value["status"] == "completed"
          end

        if selected do
          rpc = Elixir.Agent.get(holder, & &1)
          refute RpcSession.wait_idle(rpc)

          case action do
            :reset ->
              assert :ok = RpcSession.handle(rpc, %{"id" => "inside", "type" => "new_session"})

            :close ->
              refute RpcSession.close(rpc)
          end

          send(owner, :callback_finished)
        end
      end

      rpc = session(text_provider(), callback)
      Elixir.Agent.update(holder, fn _ -> rpc end)
      prompt(rpc, "go")
      assert_receive :callback_finished, 1_000

      if action == :close do
        assert RpcSession.close(rpc)
      else
        assert RpcSession.wait_idle(rpc)
        assert_receive {:rpc, %{"id" => "inside", "data" => %{"abortedRun" => true}}, nil}
        assert RpcSession.get_state(rpc)["messageCount"] == 0
      end

      assert Enum.count(messages(), &(&1["type"] == "agent_end")) == 1
    end
  end

  test "independent sessions share configuration without history or cancellation" do
    owner = self()

    provider =
      scripted(fn request, options ->
        text = List.last(request.history).text
        send(owner, {:request, text, request.history})
        if text == "wait", do: until_cancelled(options[:cancellation])
        ProviderTurn.new(text, [], [], [])
      end)

    first = session(provider)
    second = session(provider)
    prompt(first, "seed")
    RpcSession.wait_idle(first)
    prompt(first, "wait")
    assert_receive {:request, "wait", [%{text: "seed"}, %{text: "seed"}, %{text: "wait"}]}, 1_000
    prompt(second, "separate")
    RpcSession.wait_idle(second)
    assert_receive {:request, "separate", [%{text: "separate"}]}
    assert RpcSession.get_state(first)["status"] == "running"
    assert RpcSession.get_state(second)["status"] == "idle"
    assert RpcSession.cancel(first) != nil
    RpcSession.wait_idle(first)
    assert RpcSession.get_state(first)["messageCount"] == 2
    assert RpcSession.get_state(second)["messageCount"] == 2
  end

  test "native provider cancellation and deadlines do not commit session history" do
    for module <- [OpenAIProvider, AnthropicProvider], timeout <- [false, true] do
      {provider, socket} =
        TinyAgent.TestSupport.provider(module, "",
          fragments: [],
          eof: false,
          timeout: if(timeout, do: 30, else: 2_000)
        )

      rpc = session(provider)
      prompt(rpc, "wait")
      assert_receive {:waiting, ^socket}, 1_000
      unless timeout, do: RpcSession.cancel(rpc)
      assert RpcSession.wait_idle(rpc)
      assert TinyAgent.FixtureTransport.snapshot(socket).closed
      assert RpcSession.get_state(rpc)["messageCount"] == 0
      assert List.last(messages())["status"] == if(timeout, do: "failed", else: "aborted")
    end
  end

  test "linked owner exit cancels the active run and waits for cleanup" do
    observer = self()

    provider =
      scripted(fn _request, options ->
        send(observer, :provider_entered)

        try do
          until_cancelled(options[:cancellation])
        after
          send(observer, {:cleanup, self()})

          receive do
            :release -> :ok
          after
            1_000 -> flunk("Owner cleanup was not released")
          end
        end
      end)

    owner =
      spawn(fn ->
        rpc =
          RpcSession.new(Agent.new(provider, "fixture"), fn value, token ->
            send(observer, {:rpc, value, token})
          end)

        send(observer, {:session, rpc})

        receive do
          :finish -> :ok
        after
          2_000 -> :ok
        end
      end)

    assert_receive {:session, rpc}, 1_000
    monitor = Process.monitor(rpc.session)
    prompt(rpc, "wait")
    assert_receive :provider_entered, 1_000
    send(owner, :finish)
    assert_receive {:cleanup, worker}, 1_000
    refute_receive {:DOWN, ^monitor, :process, _, _}, 0
    assert Process.alive?(rpc.session)
    send(worker, :release)
    assert_receive {:DOWN, ^monitor, :process, _, :normal}, 1_000
    refute Process.alive?(worker)
    assert List.last(messages())["status"] == "aborted"
    assert RpcSession.close(rpc)
  end
end
