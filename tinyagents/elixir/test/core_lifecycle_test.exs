defmodule TinyAgent.CoreLifecycleTest do
  use ExUnit.Case, async: true
  import TinyAgent.TestSupport
  import TinyAgent.CoreSupport

  alias TinyAgent.{
    Agent,
    AnthropicProvider,
    CancellationToken,
    Cancelled,
    FixtureTransport,
    OpenAIProvider,
    ProviderError,
    ProviderTurn,
    UserMessage
  }

  test "native callback failures preserve the original exception and stop delivery" do
    for {module, prefix} <- providers(), phase <- [:start, :text_delta, :completed] do
      {provider, socket} = provider(module, fixture(prefix <> "_text"))
      failure = %RuntimeError{message: "consumer failed"}
      owner = self()

      callback = fn event, _token ->
        send(owner, {:core_event, event})

        if event.kind == phase or (is_tuple(event.kind) and elem(event.kind, 0) == phase),
          do: raise(failure)
      end

      assert {:error, ^failure} =
               capture(fn ->
                 Agent.run(Agent.new(provider, "fixture"), "go", on_event: callback)
               end)

      events = collect()
      last = List.last(events).kind
      assert last == phase or elem(last, 0) == phase
      assert FixtureTransport.snapshot(socket).closed == (phase != :start)
      assert event_keys() == []
    end
  end

  test "consumer throw and exit survive provider suppression without later delivery" do
    for failure <- [
          {:error, %RuntimeError{message: "original failure"}},
          {:throw, :consumer_throw},
          {:exit, :consumer_exit}
        ] do
      owner = self()

      provider =
        scripted(fn _request, options ->
          assert capture(fn -> options[:on_text].("first", options[:cancellation]) end) == failure

          assert capture(fn -> options[:on_text].("second", options[:cancellation]) end) ==
                   failure

          ProviderTurn.new("ignored", [], [], [])
        end)

      token = CancellationToken.new()

      callback = fn event, signal ->
        send(owner, {:core_event, event})

        if match?({:text_delta, _}, event.kind) do
          CancellationToken.cancel(signal)
          {kind, reason} = failure
          :erlang.raise(kind, reason, [])
        end
      end

      assert capture(fn ->
               Agent.run(Agent.new(provider, "fixture"), "go",
                 on_event: callback,
                 cancellation: token
               )
             end) == failure

      assert [%{kind: :start}, %{kind: {:text_delta, "first"}}] = collect()
      refute CancellationToken.cancelled?(token)
      assert event_keys() == []
    end
  end

  test "pre-cancellation emits start and abort without entering the provider" do
    token = CancellationToken.new()
    CancellationToken.cancel(token)
    provider = scripted(fn _, _ -> flunk("Pre-cancelled run entered provider") end)

    assert_raise Cancelled, fn ->
      Agent.run(Agent.new(provider, "fixture"), "go", cancellation: token, on_event: record())
    end

    assert Enum.map(collect(), & &1.kind) == [:start, :aborted]
    assert event_keys() == []
  end

  test "callback cancellation at start, delta, and final delivery leaves the parent unchanged" do
    for {module, prefix} <- providers(), phase <- [:start, :text_delta, :completed] do
      {provider, socket} = provider(module, fixture(prefix <> "_text"))
      parent = CancellationToken.new()
      owner = self()

      callback = fn event, token ->
        send(owner, {:core_event, event})

        if event.kind == phase or (is_tuple(event.kind) and elem(event.kind, 0) == phase),
          do: CancellationToken.cancel(token)
      end

      assert_raise Cancelled, fn ->
        Agent.run(Agent.new(provider, "fixture"), "go", cancellation: parent, on_event: callback)
      end

      refute CancellationToken.cancelled?(parent)
      assert FixtureTransport.snapshot(socket).closed == (phase != :start)

      terminal =
        Enum.filter(collect(), &(&1.kind == :aborted or match?({:completed, _}, &1.kind)))

      assert length(terminal) == 1

      assert hd(terminal).kind ==
               if(phase == :completed, do: {:completed, "done\ncafé"}, else: :aborted)
    end
  end

  test "providers cannot swallow run or separate callback-token cancellation and return success" do
    for separate <- [false, true], wrapped <- [false, true] do
      provider =
        scripted(fn _request, options ->
          parent = options[:cancellation]
          token = if separate, do: CancellationToken.child(parent), else: parent
          assert_raise Cancelled, fn -> options[:on_text].("cancel", token) end
          if wrapped, do: raise(ProviderError, message: "Wrapped cancellation")
          ProviderTurn.new("ignored result", [], [], [])
        end)

      parent = CancellationToken.new()
      owner = self()

      callback = fn event, token ->
        send(owner, {:core_event, event})
        if match?({:text_delta, _}, event.kind), do: CancellationToken.cancel(token)
      end

      assert_raise Cancelled, fn ->
        Agent.run(Agent.new(provider, "fixture"), "go", cancellation: parent, on_event: callback)
      end

      assert Enum.map(collect(), & &1.kind) == [:start, {:text_delta, "cancel"}, :aborted]
      refute CancellationToken.cancelled?(parent)
    end
  end

  test "native cancellation interrupts pending headers and body without consuming caller messages" do
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

          result =
            capture(fn ->
              Agent.run(Agent.new(provider, "fixture"), "go",
                cancellation: token,
                on_event: record()
              )
            end)

          assert_receive {:tcp, ^foreign, "unrelated"}
          assert_receive :caller_message
          assert Enum.map(collect(), & &1.kind) == [:start, :aborted]
          assert event_keys() == []
          result
        end)

      assert_receive {:waiting, ^socket}, 1_000
      CancellationToken.cancel(token)
      assert {:error, %Cancelled{}} = Task.await(worker, 1_000)
      assert FixtureTransport.snapshot(socket).closed
    end
  end

  test "native deadlines during receive, callback, and cleanup remain provider failures" do
    for {module, prefix} <- providers(), phase <- [:receive, :callback, :cleanup] do
      close = if phase == :cleanup, do: fn -> Process.sleep(50) end, else: nil
      options = if phase == :receive, do: [fragments: [], eof: false], else: []

      {provider, socket} =
        provider(
          module,
          fixture(prefix <> "_text"),
          options ++ [timeout: 30, fragment_size: 8192, close: close]
        )

      token = CancellationToken.new()
      owner = self()

      callback = fn event, signal ->
        send(owner, {:core_event, event})

        if phase == :callback and match?({:text_delta, _}, event.kind) do
          Process.sleep(50)
          assert CancellationToken.cancelled?(signal)
        end
      end

      assert_raise ProviderError, "Provider request timed out.", fn ->
        Agent.run(Agent.new(provider, "fixture"), "go", cancellation: token, on_event: callback)
      end

      refute CancellationToken.cancelled?(token)
      assert FixtureTransport.snapshot(socket).closed
      assert List.last(collect()).kind == {:failed, "Provider request timed out."}
    end
  end

  test "held cleanup delays terminal delivery and cancellation prevents a result" do
    owner = self()

    for {module, prefix} <- providers() do
      close = fn ->
        send(owner, {:closing, self()})

        receive do
          :release -> :ok
        after
          1_000 -> flunk("Cleanup was not released")
        end
      end

      {provider, _socket} =
        provider(module, fixture(prefix <> "_text"), close: close, fragment_size: 8192)

      token = CancellationToken.new()
      callback = record()

      worker =
        Task.async(fn ->
          capture(fn ->
            Agent.run(Agent.new(provider, "fixture"), "go",
              cancellation: token,
              on_event: callback
            )
          end)
        end)

      assert_receive {:closing, pid}, 1_000
      assert pid == worker.pid
      assert Task.yield(worker, 0) == nil
      assert match?({:text_delta, _}, List.last(collect()).kind)
      CancellationToken.cancel(token)
      send(pid, :release)
      assert {:error, %Cancelled{}} = Task.await(worker, 1_000)
      assert [%{kind: :aborted}] = collect()
    end
  end

  test "held final delivery blocks completion and cannot produce a second terminal event" do
    owner = self()

    for cancel <- [false, true] do
      provider = scripted(fn _, _ -> ProviderTurn.new("done", [], [], []) end)
      token = CancellationToken.new()

      callback = fn event, _signal ->
        send(owner, {:core_event, event})

        if match?({:completed, _}, event.kind) do
          send(owner, {:delivering, self()})

          receive do
            :release -> :ok
          after
            1_000 -> flunk("Delivery was not released")
          end
        end
      end

      worker =
        Task.async(fn ->
          capture(fn ->
            Agent.run(Agent.new(provider, "fixture"), "go",
              cancellation: token,
              on_event: callback
            )
          end)
        end)

      assert_receive {:delivering, pid}, 1_000
      assert Task.yield(worker, 0) == nil
      if cancel, do: CancellationToken.cancel(token)
      send(pid, :release)
      result = Task.await(worker, 1_000)

      if cancel,
        do: assert(match?({:error, %Cancelled{}}, result)),
        else: assert(match?({:ok, %{text: "done"}}, result))

      assert Enum.map(collect(), & &1.kind) == [
               :start,
               {:text_delta, "done"},
               {:completed, "done"}
             ]
    end
  end

  test "one configured agent supports independent concurrent runs" do
    owner = self()

    provider =
      scripted(fn request, options ->
        prompt = List.last(request.history).text
        options[:on_text].(prompt, options[:cancellation])
        send(owner, {:running, self(), request.history})

        receive do
          :release -> :ok
        after
          1_000 -> flunk("Run was not released")
        end

        CancellationToken.check!(options[:cancellation])
        ProviderTurn.new(prompt, [], [], [])
      end)

    agent = Agent.new(provider, "fixture")
    first_token = CancellationToken.new()
    second_token = CancellationToken.new()
    callback = record()

    first =
      Task.async(fn ->
        capture(fn ->
          Agent.run(agent, "one",
            history: [UserMessage.new("seed")],
            run_id: "first",
            cancellation: first_token,
            on_event: callback
          )
        end)
      end)

    second =
      Task.async(fn ->
        capture(fn ->
          Agent.run(agent, "two",
            run_id: "second",
            cancellation: second_token,
            on_event: callback
          )
        end)
      end)

    first_pid = first.pid
    second_pid = second.pid
    assert_receive {:running, ^first_pid, [%{text: "seed"}, %{text: "one"}]}
    assert_receive {:running, ^second_pid, [%{text: "two"}]}
    CancellationToken.cancel(first_token)
    send(first.pid, :release)
    send(second.pid, :release)
    assert {:error, %Cancelled{}} = Task.await(first, 1_000)

    assert {:ok, %{text: "two", history: [%{text: "two"}, %{text: "two"}]}} =
             Task.await(second, 1_000)

    refute CancellationToken.cancelled?(second_token)
    events = Enum.group_by(collect(), & &1.run_id)
    assert List.last(events["first"]).kind == :aborted
    assert List.last(events["second"]).kind == {:completed, "two"}
    for {_id, values} <- events, do: assert(Enum.map(values, & &1.sequence) == [1, 2, 3])
  end

  test "nested runs preserve caller state and retained callbacks cannot emit later" do
    marker = make_ref()
    Process.put(marker, "caller state")
    owner = self()

    provider =
      scripted(fn request, options ->
        send(owner, {:retained, fn -> options[:on_text].("late", options[:cancellation]) end})
        ProviderTurn.new(List.last(request.history).text, [], [], [])
      end)

    agent = Agent.new(provider, "fixture")

    callback = fn event, _token ->
      send(owner, {:core_event, event})

      if event.kind == :start do
        assert length(event_keys()) == 1
        assert Agent.run(agent, "inner", on_event: record(), run_id: "inner").text == "inner"
        assert length(event_keys()) == 1
      end
    end

    assert Agent.run(agent, "outer", on_event: callback, run_id: "outer").text == "outer"
    events = collect()

    assert Enum.map(events, &{&1.run_id, &1.sequence}) == [
             {"outer", 1},
             {"inner", 1},
             {"inner", 2},
             {"inner", 3},
             {"outer", 2},
             {"outer", 3}
           ]

    for _ <- 1..2 do
      assert_receive {:retained, callback}
      assert_raise Cancelled, callback
    end

    assert collect() == []
    assert event_keys() == []
    assert Process.delete(marker) == "caller state"
  end

  defp providers, do: [{OpenAIProvider, "openai"}, {AnthropicProvider, "anthropic"}]
end
