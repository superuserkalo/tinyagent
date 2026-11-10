defmodule TinyAgent.SessionTest do
  use ExUnit.Case, async: true
  import TinyAgent.CoreSupport
  import TinyAgent.SessionSupport
  alias TinyAgent.{CancellationToken, ProviderTurn, RpcSession, SessionError}

  test "parsed command replies match shared RPC fixtures" do
    scenarios =
      Path.expand("../../../conformance/rpc_v1.json", __DIR__)
      |> File.read!()
      |> Jason.decode!()
      |> Map.fetch!("scenarios")

    for scenario <- scenarios, expected = scenario["expected"] do
      rpc = session(text_provider())
      for command <- scenario["commands"], do: assert(:ok = RpcSession.handle(rpc, command))
      assert messages() == expected
      assert RpcSession.close(rpc)
      refute Process.alive?(rpc.session)
    end
  end

  test "acknowledgements precede ordered events and completed history" do
    rpc = session(text_provider())
    assert :ok = prompt(rpc, "go")
    assert RpcSession.wait_idle(rpc)

    assert_receive {:rpc,
                    %{"type" => "response", "command" => "prompt", "data" => %{"runId" => id}},
                    %CancellationToken{}}

    events = messages()
    assert Enum.map(events, & &1["type"]) == ["agent_start", "text_delta", "agent_end"]
    assert Enum.map(events, & &1["sequence"]) == [1, 2, 3]
    assert Enum.all?(events, &(&1["runId"] == id))
    assert List.last(events)["status"] == "completed"

    assert RpcSession.get_state(rpc) == %{
             "protocolVersion" => 1,
             "status" => "idle",
             "runId" => nil,
             "messageCount" => 2
           }
  end

  test "pending acknowledgement holds provider entry but permits busy, state, and abort" do
    owner = self()

    provider =
      scripted(fn _, _ ->
        send(owner, :provider_entered)
        ProviderTurn.new("done", [], [], [])
      end)

    emit = fn value, token ->
      send(owner, {:rpc, value, token})

      if value["command"] == "prompt" and value["success"] do
        send(owner, {:acknowledging, self(), token})

        receive do
          :release -> :ok
        after
          1_000 -> flunk("Acknowledgement was not released")
        end
      end
    end

    rpc = session(provider, emit)
    prompt(rpc, "wait", "first")
    assert_receive {:acknowledging, worker, token}, 1_000
    assert RpcSession.get_state(rpc)["status"] == "running"
    refute_receive :provider_entered, 0
    prompt(rpc, "busy", "second")
    assert_receive {:rpc, %{"id" => "second", "error" => %{"code" => "busy"}}, nil}
    RpcSession.handle(rpc, %{"id" => "abort", "type" => "abort"})
    assert_receive {:rpc, %{"id" => "abort", "data" => %{"accepted" => true}}, nil}
    assert CancellationToken.cancelled?(token)
    send(worker, :release)
    assert RpcSession.wait_idle(rpc)
    refute_receive :provider_entered, 0
    assert List.last(messages())["status"] == "aborted"
    assert RpcSession.get_state(rpc)["messageCount"] == 0
  end

  test "failed acknowledgement never calls the core or commits history" do
    owner = self()

    provider =
      scripted(fn _, _ ->
        send(owner, :provider_entered)
        ProviderTurn.new("done", [], [], [])
      end)

    for kind <- [:error, :throw, :exit] do
      rpc =
        session(provider, fn value, _token ->
          send(owner, {:ack_attempt, value})

          :erlang.raise(
            kind,
            if(kind == :error,
              do: %RuntimeError{message: "delivery failed"},
              else: :delivery_failed
            ),
            []
          )
        end)

      prompt(rpc, "go")
      assert RpcSession.wait_idle(rpc)
      assert_receive {:ack_attempt, %{"command" => "prompt"}}
      refute_receive {:ack_attempt, _}, 0
      refute_receive :provider_entered, 0
      assert RpcSession.get_state(rpc)["messageCount"] == 0
    end
  end

  test "provider errors, cancellation, and consumer failures retain prior history" do
    owner = self()

    provider =
      scripted(fn request, options ->
        text = List.last(request.history).text
        send(owner, {:history, text, request.history})

        case text do
          "error" -> raise "provider failed"
          "cancel" -> CancellationToken.cancel(options[:cancellation])
          _ -> :ok
        end

        options[:on_text].(text, options[:cancellation])
        ProviderTurn.new(text, [], [], [])
      end)

    rpc =
      session(provider, fn value, token ->
        send(owner, {:rpc, value, token})

        if value["status"] == "completed" and value["text"] == "bad delivery",
          do: raise("consumer failed")
      end)

    prompt(rpc, "seed")
    RpcSession.wait_idle(rpc)
    assert RpcSession.get_state(rpc)["messageCount"] == 2
    assert_receive {:history, "seed", [%{text: "seed"}]}
    messages()

    for {text, status} <- [
          {"error", "failed"},
          {"cancel", "aborted"},
          {"bad delivery", "completed"}
        ] do
      prompt(rpc, text)
      RpcSession.wait_idle(rpc)
      assert_receive {:history, ^text, [%{text: "seed"}, %{text: "seed"}, %{text: ^text}]}
      assert RpcSession.get_state(rpc)["messageCount"] == 2
      values = messages()
      assert List.last(values)["status"] == status
      assert Enum.count(values, &(&1["type"] == "agent_end")) == 1
    end
  end

  test "final delivery holds admission and history commit" do
    owner = self()

    rpc =
      session(text_provider(), fn value, token ->
        send(owner, {:rpc, value, token})

        if value["status"] == "completed" do
          send(owner, {:delivering, self()})

          receive do
            :release -> :ok
          after
            1_000 -> flunk("Final delivery was not released")
          end
        end
      end)

    prompt(rpc, "go")
    assert_receive {:delivering, worker}, 1_000
    assert RpcSession.get_state(rpc)["status"] == "running"
    assert RpcSession.get_state(rpc)["messageCount"] == 0
    prompt(rpc, "busy", "busy")
    assert_receive {:rpc, %{"id" => "busy", "error" => %{"code" => "busy"}}, nil}
    send(worker, :release)
    RpcSession.wait_idle(rpc)
    assert RpcSession.get_state(rpc)["messageCount"] == 2
  end

  test "invalid command values and fields never start a provider" do
    owner = self()

    rpc =
      session(
        scripted(fn _, _ ->
          send(owner, :provider_entered)
          ProviderTurn.new("", [], [], [])
        end)
      )

    for value <- [nil, [], "text", %TinyAgent.UserMessage{text: "go"}] do
      RpcSession.handle(rpc, value)
      assert_receive {:rpc, %{"error" => %{"code" => "invalid_command"}}, nil}
    end

    deep = Enum.reduce(1..101, nil, fn _, value -> %{"nested" => value} end)

    for invalid <- [:atom, self(), make_ref(), fn -> :ok end, {1, 2}, [1 | 2], <<255>>, deep] do
      RpcSession.handle(rpc, %{"type" => "get_state", "extra" => invalid})
      assert_receive {:rpc, %{"error" => %{"code" => "invalid_json"}}, nil}
    end

    RpcSession.handle(rpc, %{type: "get_state"})
    assert_receive {:rpc, %{"error" => %{"code" => "invalid_json"}}, nil}

    for id <- [nil, 42, false] do
      RpcSession.handle(rpc, %{"type" => "get_state", "id" => id})
      assert_receive {:rpc, %{"error" => %{"code" => "invalid_command"}}, nil}
    end

    RpcSession.handle(rpc, %{"type" => "get_state", "id" => "line\ncafé"})
    assert_receive {:rpc, %{"id" => "line\ncafé", "success" => true}, nil}
    refute_receive :provider_entered, 0
  end

  test "ordinary command delivery failures propagate unchanged" do
    owner = self()

    for failure <- [
          %RuntimeError{message: "command delivery failed"},
          %SessionError{reason: :busy, message: "emitter failed"},
          %SessionError{reason: :closed, message: "emitter closed"}
        ] do
      rpc =
        session(text_provider(), fn _, _ ->
          send(owner, :delivery_attempt)
          raise failure
        end)

      assert {:error, ^failure} =
               capture(fn -> RpcSession.handle(rpc, %{"type" => "get_state"}) end)

      assert_receive :delivery_attempt
      refute_receive :delivery_attempt, 0
      assert RpcSession.get_state(rpc)["status"] == "idle"
    end
  end

  test "stop closes admission and close is idempotent and joins the controller" do
    rpc = session(text_provider())
    assert RpcSession.stop(rpc) == nil
    prompt(rpc, "closed")
    assert_receive {:rpc, %{"error" => %{"code" => "session_closed"}}, nil}
    assert RpcSession.close(rpc)
    refute Process.alive?(rpc.session)
    assert RpcSession.close(rpc)
    prompt(rpc, "closed again")
    assert_receive {:rpc, %{"error" => %{"code" => "session_closed"}}, nil}
    assert_raise SessionError, fn -> RpcSession.get_state(rpc) end
  end
end
