defmodule TinyAgent.CoreTest do
  use ExUnit.Case, async: true
  import TinyAgent.TestSupport
  import TinyAgent.CoreSupport

  alias TinyAgent.{
    Agent,
    AgentEvent,
    AnthropicProvider,
    AssistantMessage,
    FixtureTransport,
    OpenAIProvider,
    ProviderError,
    ProviderTurn,
    RunResult,
    UserMessage
  }

  for {module, prefix} <- [{OpenAIProvider, "openai"}, {AnthropicProvider, "anthropic"}] do
    test "#{prefix} core emits ordered events after native response cleanup and replays history" do
      module = unquote(module)
      prefix = unquote(prefix)

      {provider, socket} =
        provider(module, fixture(prefix <> "_text"), framing: :open, eof: false)

      agent = Agent.new(provider, "native-model", instructions: "Native instructions")
      owner = self()

      result =
        Agent.run(agent, "first",
          run_id: "native-run",
          on_event: fn event, _token ->
            assert self() == owner
            assert FixtureTransport.snapshot(socket).closed == match?({:completed, _}, event.kind)
            send(owner, {:core_event, event})
          end
        )

      assert result.text == "done\ncafé"
      assert [%UserMessage{text: "first"}, %AssistantMessage{}] = result.history
      events = collect()
      assert Enum.map(events, & &1.sequence) == Enum.to_list(1..length(events))
      assert Enum.all?(events, &(&1.run_id == "native-run"))
      assert hd(events).kind == :start
      assert List.last(events).kind == {:completed, result.text}
      assert for(%{kind: {:text_delta, text}} <- events, into: "", do: text) == result.text
      {_, sent} = sent(socket)
      assert sent["model"] == "native-model"
      assert (sent["system"] || sent["instructions"]) == "Native instructions"
      assert Map.get(sent, "tools", []) == []

      {next, socket} = provider(module, fixture(prefix <> "_text"))
      again = Agent.run(Agent.new(next, "native-model"), "second", history: result.history)
      assert length(again.history) == 4
      assert length(result.history) == 2
      {_, sent} = sent(socket)
      history = sent["input"] || sent["messages"]
      assert length(history) == 3
      assert List.last(history)["content"] == "second"
      assert Enum.at(history, 1)["content"] |> hd() |> Map.fetch!("text") == result.text
    end
  end

  test "events have immutable shared protocol objects" do
    fields = [
      {:start, %{"type" => "agent_start"}},
      {{:text_delta, "hello\ncafé"}, %{"type" => "text_delta", "delta" => "hello\ncafé"}},
      {{:completed, "answer"},
       %{"type" => "agent_end", "status" => "completed", "text" => "answer"}},
      {:aborted, %{"type" => "agent_end", "status" => "aborted"}},
      {{:failed, "failed"},
       %{
         "type" => "agent_end",
         "status" => "failed",
         "error" => %{"code" => "provider_error", "message" => "failed", "retryable" => false}
       }}
    ]

    for {{kind, expected}, index} <- Enum.with_index(fields, 1) do
      event = AgentEvent.new("run", index, kind)
      value = AgentEvent.to_map(event)
      assert value == Map.merge(expected, %{"runId" => "run", "sequence" => index})
      assert value == value |> Jason.encode!() |> Jason.decode!()
      assert Map.put(value, "runId", "changed")["runId"] == "changed"
      assert event.run_id == "run"
    end

    assert_raise FunctionClauseError, fn -> AgentEvent.new("", 1, :start) end
    assert_raise FunctionClauseError, fn -> AgentEvent.new("run", 0, :start) end
    assert_raise ArgumentError, fn -> AgentEvent.new("run", 1, {:text_delta, <<255>>}) end
    assert_raise ArgumentError, fn -> apply(AgentEvent, :new, ["run", 1, :unknown]) end
  end

  test "completed history and output retain immutable JSON values" do
    data = %{"nested" => %{"value" => "keep"}, "empty" => %{}, "array" => []}
    prior = %AssistantMessage{text: "previous", replay: [data]}
    turn = ProviderTurn.new("answer", [], [data], [data])

    agent =
      Agent.new(
        scripted(fn request, _options ->
          assert request.history == [prior, UserMessage.new("go")]
          turn
        end),
        "fixture"
      )

    result = Agent.run(agent, "go", history: [prior])
    changed = put_in(data["nested"]["value"], "changed")
    assert changed != data
    assert result.output == [data]
    assert hd(result.history).replay == [data]
    assert List.last(result.history).replay == [data]
    assert turn.output == [data]
    assert_raise ArgumentError, fn -> RunResult.new("answer", [42], []) end
    assert_raise ProviderError, fn -> RunResult.new("answer", [], [42]) end
  end

  test "final answer is distinct from streamed text and blank deltas are omitted" do
    provider =
      scripted(fn _request, options ->
        options[:on_text].("", options[:cancellation])
        options[:on_text].("intermediate", options[:cancellation])
        ProviderTurn.new("answer", [], [], [])
      end)

    result = Agent.run(Agent.new(provider, "fixture"), "", on_event: record())
    assert result.text == "answer"

    assert Enum.map(collect(), & &1.kind) == [
             :start,
             {:text_delta, "intermediate"},
             {:completed, "answer"}
           ]
  end

  test "provider failures preserve raise, throw, and exit after one terminal event" do
    for failure <- [
          {:error, %RuntimeError{message: "provider failed"}},
          {:throw, :thrown},
          {:exit, :exited}
        ],
        partial <- [false, true] do
      owner = self()

      provider =
        scripted(fn _request, options ->
          try do
            if partial, do: options[:on_text].("partial", options[:cancellation])
            {kind, reason} = failure
            :erlang.raise(kind, reason, [])
          after
            send(owner, :provider_closed)
          end
        end)

      callback = fn event, _token ->
        if match?({:failed, _}, event.kind), do: assert_receive(:provider_closed)
        send(owner, {:core_event, event})
      end

      assert capture(fn ->
               Agent.run(Agent.new(provider, "fixture"), "go", on_event: callback)
             end) == failure

      events = collect()
      assert length(events) == if(partial, do: 3, else: 2)
      assert match?({:failed, _}, List.last(events).kind)
      assert event_keys() == []
    end
  end

  test "tool calls fail as unregistered after native cleanup" do
    for {module, prefix} <- [{OpenAIProvider, "openai"}, {AnthropicProvider, "anthropic"}] do
      {provider, socket} = provider(module, fixture(prefix <> "_tool"))

      assert_raise ProviderError, ~r/unregistered tool/, fn ->
        Agent.run(Agent.new(provider, "fixture"), "go", on_event: record())
      end

      assert FixtureTransport.snapshot(socket).closed
      assert match?({:failed, _}, List.last(collect()).kind)
    end
  end

  test "invalid inputs fail before provider entry and generated IDs are distinct" do
    provider = scripted(fn _request, _options -> flunk("Invalid input entered provider") end)

    for model <- ["", 42, <<255>>],
        do: assert_raise(ArgumentError, fn -> Agent.new(provider, model) end)

    assert_raise ArgumentError, fn -> apply(Agent, :new, [%{}, "fixture"]) end
    assert_raise ArgumentError, fn -> Agent.new(provider, "fixture", instructions: <<255>>) end
    agent = Agent.new(provider, "fixture")

    for prompt <- [42, <<255>>],
        do: assert_raise(ArgumentError, fn -> Agent.run(agent, prompt) end)

    for id <- [false, "", 42, <<255>>],
        do: assert_raise(ArgumentError, fn -> Agent.run(agent, "go", run_id: id) end)

    for options <- [
          [history: [42]],
          [on_event: fn _ -> :ok end],
          [cancellation: false],
          [unknown: true]
        ],
        do: assert_raise(ArgumentError, fn -> Agent.run(agent, "go", options) end)

    agent = Agent.new(scripted(fn _, _ -> ProviderTurn.new("", [], [], []) end), "fixture")

    ids =
      for _ <- 1..2 do
        assert Agent.run(agent, "", on_event: record()).text == ""
        events = collect()
        id = hd(events).run_id
        assert id =~ ~r/^r_[a-f0-9]{24}$/
        id
      end

    assert length(Enum.uniq(ids)) == 2
    assert event_keys() == []
  end

  test "invalid provider turns and UTF-8 produce valid failure events" do
    for value <- [
          nil,
          %{},
          ProviderTurn.new(<<255>>, [], [], []),
          ProviderTurn.new("", [], [], [42])
        ] do
      provider = scripted(fn _, _ -> value end)

      assert {:error, _} =
               capture(fn ->
                 Agent.run(Agent.new(provider, "fixture"), "go", on_event: record())
               end)

      assert [%{kind: :start}, %{kind: {:failed, _}} = failure] = collect()
      assert failure |> AgentEvent.to_map() |> Jason.encode!() |> is_binary()
    end

    provider =
      scripted(fn _, options ->
        options[:on_text].(<<255>>, options[:cancellation])
        flunk("Invalid delta returned")
      end)

    assert_raise ArgumentError, fn ->
      Agent.run(Agent.new(provider, "fixture"), "go", on_event: record())
    end

    assert [%{kind: :start}, %{kind: {:failed, _}}] = collect()
    provider = scripted(fn _, _ -> raise RuntimeError, message: <<255>> end)

    assert_raise RuntimeError, fn ->
      Agent.run(Agent.new(provider, "fixture"), "go", on_event: record())
    end

    assert [%{kind: :start}, %{kind: {:failed, "Provider request failed."}}] = collect()
  end
end
