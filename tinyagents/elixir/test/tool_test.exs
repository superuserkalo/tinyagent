defmodule TinyAgent.ToolTest do
  use ExUnit.Case, async: true
  import TinyAgent.CoreSupport
  import TinyAgent.TestSupport

  alias TinyAgent.{
    Agent,
    AgentEvent,
    AnthropicProvider,
    CancellationToken,
    Cancelled,
    FixtureTransport,
    OpenAIProvider,
    ProviderError,
    ProviderTurn,
    Tool,
    ToolCall,
    ToolResultMessage,
    UserMessage
  }

  defp echo(handler) do
    Tool.new(
      "echo",
      "Return text",
      %{
        "type" => "object",
        "properties" => %{"text" => %{"type" => "string", "minLength" => 1}},
        "required" => ["text"],
        "additionalProperties" => false
      },
      handler
    )
  end

  defp call(text, id \\ "call"), do: ToolCall.new(id, "echo", %{"text" => text})

  defp agent(tool, calls) do
    provider =
      scripted(fn request, _options ->
        if match?(%UserMessage{}, List.last(request.history)),
          do: ProviderTurn.new("working", calls, [], []),
          else: ProviderTurn.new("done", [], [], [])
      end)

    Agent.new(provider, "fixture", tools: [tool])
  end

  test "all shared argument schemas validate before handler entry" do
    fixture =
      Jason.decode!(File.read!(Path.expand("../../../conformance/tool_arguments.json", __DIR__)))

    owner = self()

    for value <- fixture["cases"] do
      tool =
        Tool.new("echo", "", value["schema"], fn arguments, _ ->
          send(owner, {:arguments, arguments})
          "ok"
        end)

      for arguments <- value["valid"] do
        assert Tool.call(tool, arguments, CancellationToken.new()) == "ok"
        assert_receive {:arguments, ^arguments}
      end

      for arguments <- value["invalid"],
          do:
            assert_raise(ArgumentError, fn ->
              Tool.call(tool, arguments, CancellationToken.new())
            end)

      refute_receive {:arguments, _}, 0
    end
  end

  test "invalid schemas, missing references and nonprogressing cycles fail at construction" do
    for json <- [
          ~s({"type":"bad"}),
          ~s({"properties":[]}),
          ~s({"required":"text"}),
          ~s({"required":["x","x"]}),
          ~s({"$defs":{"unused":{"type":"bad"}}}),
          ~s({"properties":{"unused":{"$ref":"https://invalid.example"}}}),
          ~s({"$ref":"#/$defs/missing"}),
          ~s({"$ref":"#"}),
          ~s({"allOf":[{"$ref":"#"}]}),
          ~s({"$defs":{"unused":{"$ref":"#/$defs/unused"}}}),
          ~s({"$dynamicAnchor":"node","$dynamicRef":"#node"}),
          ~s({"$schema":"http://json-schema.org/draft-07/schema#"}),
          ~s({"$schema":"https://invalid.example"}),
          ~s({"$ref":"jsv:module:Elixir.JSV.Resolver.Embedded.Draft202012.Schema"})
        ] do
      assert {:error, _} =
               capture(fn -> Tool.new("echo", "", Jason.decode!(json), fn _, _ -> "ok" end) end)
    end

    assert Tool.call(Tool.new("yes", "", true, fn _, _ -> "ok" end), %{}, CancellationToken.new()) ==
             "ok"

    assert_raise ArgumentError, fn ->
      Tool.call(Tool.new("no", "", false, fn _, _ -> "bad" end), %{}, CancellationToken.new())
    end
  end

  test "annotations, defaults, immutable arguments and duplicate registration" do
    schema = %{
      "type" => "object",
      "properties" => %{
        "n" => %{"type" => "integer", "default" => 4},
        "email" => %{"format" => "email"}
      },
      "examples" => [%{"$ref" => "https://annotation.invalid"}]
    }

    tool =
      Tool.new("echo", "", schema, fn arguments, _ ->
        assert arguments == %{"email" => "not an email"}
        "ok"
      end)

    assert Tool.call(tool, %{"email" => "not an email"}, CancellationToken.new()) == "ok"
    assert_raise ArgumentError, fn -> Tool.call(tool, %{"n" => "4"}, CancellationToken.new()) end

    assert_raise ArgumentError, fn ->
      Agent.new(scripted(fn _, _ -> nil end), "fixture", tools: [tool, tool])
    end

    assert tool.definition.parameters == schema
  end

  for {module, prefix} <- [{OpenAIProvider, "openai"}, {AnthropicProvider, "anthropic"}] do
    test "#{prefix} native tool loop cleans the response and preserves replay" do
      {first, socket} =
        provider(unquote(module), fixture(unquote(prefix) <> "_tool"), framing: :open, eof: false)

      {second, replay} =
        provider(unquote(module), fixture(unquote(prefix) <> "_text"), framing: :open, eof: false)

      provider =
        scripted(fn request, options ->
          selected =
            if match?(%UserMessage{}, List.last(request.history)), do: first, else: second

          selected.__struct__.stream(selected, request, options)
        end)

      tool =
        echo(fn arguments, _ ->
          assert FixtureTransport.snapshot(socket).closed
          assert arguments == %{"text" => "café"}
          "native café"
        end)

      result = Agent.run(Agent.new(provider, "fixture", tools: [tool]), "go", on_event: record())
      assert length(result.history) == 4
      assert Enum.at(result.history, 2).output == "native café"
      assert FixtureTransport.snapshot(replay).closed
      {_, payload} = sent(replay)
      assert hd(payload["tools"])["name"] == "echo"

      if unquote(prefix) == "openai" do
        assert Enum.at(payload["input"], 1)["encrypted_content"] == "opaque-fixture"
        assert Enum.at(payload["input"], 3)["output"] == "native café"
      else
        assert Enum.at(payload["messages"], 1)["content"] |> hd() |> Map.fetch!("signature") ==
                 "signed-fixture"

        assert Enum.at(payload["messages"], 2)["content"] |> hd() |> Map.fetch!("content") ==
                 "native café"
      end

      events = collect()
      assert Enum.count(events, &match?({:tool_start, _, _, _}, &1.kind)) == 1
      assert Enum.count(events, &match?({:tool_end, _, _, _, _}, &1.kind)) == 1
    end
  end

  test "sequential errors and oversized or invalid outputs recover with ordered events" do
    owner = self()

    tool =
      echo(fn %{"text" => text}, _ ->
        send(owner, {:handled, text})

        case text do
          "throw" -> raise "a" <> String.duplicate("\u0301", 100_000)
          "large" -> String.duplicate("é", 131_072)
          "invalid" -> <<255>>
          _ -> text
        end
      end)

    calls =
      Enum.with_index(["first", "throw", "large", "invalid", "", "last"], fn text, index ->
        call(text, "call-#{index}")
      end)

    result = Agent.run(agent(tool, calls), "go", on_event: record())

    for text <- ["first", "throw", "large", "invalid", "last"],
        do: assert_receive({:handled, ^text})

    refute_receive {:handled, _}, 0
    observations = Enum.filter(result.history, &is_struct(&1, ToolResultMessage))
    assert Enum.map(observations, & &1.is_error) == [false, true, true, true, true, false]

    assert Enum.all?(
             observations,
             &(String.valid?(&1.output) and byte_size(&1.output) <= 131_072)
           )

    events = collect()
    assert Enum.map(events, & &1.sequence) == Enum.to_list(1..length(events))
    assert length(Enum.uniq(Enum.map(events, & &1.run_id))) == 1
    assert for(%{kind: {:text_delta, text}} <- events, do: text) == ["working", "done"]
    assert Enum.count(events, &match?({:tool_start, _, _, _}, &1.kind)) == 6
    assert Enum.count(events, &match?({:tool_end, _, _, _, _}, &1.kind)) == 6
    assert List.last(events).kind == {:completed, "done"}
    for event <- events, do: assert(is_binary(Jason.encode!(AgentEvent.to_map(event))))
  end

  test "unknown names reject the whole batch before handler entry" do
    tool = echo(fn _, _ -> flunk("Handler ran") end)

    assert_raise ProviderError, fn ->
      Agent.run(agent(tool, [call("ok"), ToolCall.new("bad", "missing", %{})]), "go",
        on_event: record()
      )
    end

    refute Enum.any?(collect(), &match?({:tool_start, _, _, _}, &1.kind))
  end

  test "cancellation during start, handler and end pairs tools and stays scoped" do
    for phase <- [:start, :handler, :end] do
      parent = CancellationToken.new()
      owner = self()

      tool =
        echo(fn _, token ->
          send(owner, :entered)

          if phase == :handler do
            CancellationToken.cancel(token)
            raise "wrapped cancellation"
          end

          "ok"
        end)

      assert_raise Cancelled, fn ->
        Agent.run(agent(tool, [call("ok")]), "go",
          cancellation: parent,
          on_event: fn event, token ->
            send(owner, {:core_event, event})

            if (phase == :start and match?({:tool_start, _, _, _}, event.kind)) or
                 (phase == :end and match?({:tool_end, _, _, _, _}, event.kind)),
               do: CancellationToken.cancel(token)
          end
        )
      end

      if phase == :start, do: refute_receive(:entered, 0), else: assert_receive(:entered)
      refute CancellationToken.cancelled?(parent)
      events = collect()
      assert Enum.count(events, &match?({:tool_start, _, _, _}, &1.kind)) == 1
      assert Enum.count(events, &match?({:tool_end, _, _, _, _}, &1.kind)) == 1
      assert List.last(events).kind == :aborted
    end
  end

  test "held tool cleanup retains run ownership before the paired end" do
    parent = CancellationToken.new()
    owner = self()

    tool =
      echo(fn _, token ->
        send(owner, :entered)

        try do
          TinyAgent.HttpSupport.wait_until(fn -> CancellationToken.cancelled?(token) end)
          CancellationToken.check!(token)
        after
          send(owner, :cleaning)
          receive do: (:release -> :ok)
          send(owner, :closed)
        end
      end)

    worker =
      Task.async(fn ->
        capture(fn ->
          Agent.run(agent(tool, [call("ok")]), "go",
            cancellation: parent,
            on_event: record_to(owner)
          )
        end)
      end)

    try do
      assert_receive :entered, 1000
      CancellationToken.cancel(parent)
      assert_receive :cleaning, 1000
      assert Task.yield(worker, 20) == nil
      refute Enum.any?(collect(), &match?({:tool_end, _, _, _, _}, &1.kind))
    after
      send(worker.pid, :release)
    end

    assert {:error, %Cancelled{}} = Task.await(worker, 1000)
    assert_receive :closed
    assert [%{kind: {:tool_end, _, _, _, true}}, %{kind: :aborted}] = collect()
  end

  test "consumer raise, throw and exit stop delivery without becoming tool errors" do
    for phase <- [:tool_start, :tool_end],
        failure <- [
          {:error, %RuntimeError{message: "consumer"}},
          {:throw, :consumer},
          {:exit, :consumer}
        ] do
      owner = self()
      tool = echo(fn _, _ -> "ok" end)

      result =
        capture(fn ->
          Agent.run(agent(tool, [call("ok")]), "go",
            on_event: fn event, _ ->
              send(owner, {:core_event, event})

              if is_tuple(event.kind) and elem(event.kind, 0) == phase do
                {kind, reason} = failure
                :erlang.raise(kind, reason, [])
              end
            end
          )
        end)

      assert result == failure
      assert collect() |> List.last() |> Map.fetch!(:kind) |> elem(0) == phase
      assert event_keys() == []
    end
  end

  test "retained callbacks reject delivery between requests without cancelling later work" do
    key = make_ref()

    provider =
      scripted(fn request, options ->
        if match?(%UserMessage{}, List.last(request.history)) do
          Process.put(key, options)
          ProviderTurn.new("", [call("ok")], [], [])
        else
          old = Process.get(key)
          assert_raise Cancelled, fn -> old[:on_text].("late", old[:cancellation]) end
          ProviderTurn.new("done", [], [], [])
        end
      end)

    tool =
      echo(fn _, _ ->
        old = Process.get(key)
        assert_raise Cancelled, fn -> old[:on_text].("late", old[:cancellation]) end
        "ok"
      end)

    assert Agent.run(Agent.new(provider, "fixture", tools: [tool]), "go").text == "done"
    old = Process.delete(key)
    assert_raise Cancelled, fn -> old[:on_text].("late", old[:cancellation]) end
  end

  defp record_to(owner), do: fn event, _ -> send(owner, {:core_event, event}) end
end
