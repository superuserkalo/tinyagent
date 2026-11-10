defmodule TinyAgent.CLITest do
  use ExUnit.Case, async: true
  import TinyAgent.CoreSupport
  import TinyAgent.TestSupport

  alias TinyAgent.{
    Bash,
    CLI,
    CancellationToken,
    Cancelled,
    FixtureTransport,
    OpenAIProvider,
    ProviderTurn,
    ToolCall,
    ToolResultMessage,
    UserMessage
  }

  test "shared Bash observations, sequential calls, and immutable replay" do
    owner = self()

    calls =
      Enum.map(
        [
          {"success", %{"command" => "printf hello"}},
          {"denied", %{"command" => "rm file"}},
          {"invalid", %{"command" => true}},
          {"large", %{"command" => "printf '%0140000d' 0"}}
        ],
        fn {id, arguments} -> ToolCall.new(id, "bash", arguments) end
      )

    provider =
      scripted(fn request, options ->
        send(owner, {:request, request})

        if match?(%UserMessage{}, List.last(request.history)) do
          ProviderTurn.new("", calls, [%{"opaque" => true}], [])
        else
          options[:on_text].("finished", options[:cancellation])
          ProviderTurn.new("finished", [], [], [])
        end
      end)

    initial = [UserMessage.new("kept")]

    result =
      CLI.turn(initial, "go", provider, "fixture",
        on_text: fn text, _ -> send(owner, {:text, text}) end
      )

    assert initial == [UserMessage.new("kept")]

    assert_receive {:request,
                    %{history: [%{text: "kept"}, %{text: "go"}], tools: [%{name: "bash"}]}}

    assert_receive {:request, %{history: next}}
    assert_receive {:text, "finished"}
    assert Enum.at(result, 2).replay == [%{"opaque" => true}]
    observations = Enum.filter(next, &is_struct(&1, ToolResultMessage))
    assert Enum.map(observations, & &1.call_id) == Enum.map(calls, & &1.call_id)
    assert Enum.at(observations, 0).output == "exit_code: 0\nstdout:\nhellostderr:\n"
    assert Enum.at(observations, 1).output =~ "exit_code: 126"
    assert Enum.at(observations, 2).is_error
    assert Enum.at(observations, 3).is_error
    assert Enum.at(observations, 3).output =~ "output exceeded"
    assert Enum.all?(observations, &(byte_size(&1.output) <= Bash.max_output_bytes()))
    assert List.last(result).text == "finished"
  end

  test "native streaming provider sends the Bash schema and closes its request" do
    {provider, socket} = provider(OpenAIProvider, fixture("openai_text"))
    owner = self()

    result =
      CLI.turn([], "go", provider, "fixture",
        on_text: fn text, _ -> send(owner, {:text, text}) end
      )

    assert List.last(result).text == "done\ncafé"
    assert_receive {:text, _}
    assert FixtureTransport.snapshot(socket).closed
    {_headers, body} = sent(socket)
    assert body["stream"] and body["store"] == false
    assert [%{"name" => "bash"}] = body["tools"]
  end

  test "provider and callback failures retain their original value without returning history" do
    failure = %RuntimeError{message: "original"}

    for callback <- [false, true] do
      provider =
        scripted(fn _, options ->
          options[:on_text].("partial", options[:cancellation])
          raise failure
        end)

      initial = [UserMessage.new("kept")]

      assert capture(fn ->
               CLI.turn(initial, "failed", provider, "fixture",
                 on_text: fn _, _ -> if callback, do: raise(failure) end
               )
             end) == {:error, failure}

      assert initial == [UserMessage.new("kept")]
    end
  end

  test "request cancellation closes the native connection without consuming caller messages" do
    {provider, socket} = provider(OpenAIProvider, "", fragments: [], eof: false)
    token = CancellationToken.new()

    worker =
      Task.async(fn ->
        send(self(), :unrelated)
        result = capture(fn -> CLI.turn([], "go", provider, "fixture", cancellation: token) end)
        assert_receive :unrelated
        result
      end)

    assert_receive {:waiting, ^socket}, 1_000
    CancellationToken.cancel(token)
    assert {:error, %Cancelled{}} = Task.await(worker, 1_000)
    assert FixtureTransport.snapshot(socket).closed
  end

  for mode <- ~w(conversation rollback idle request bash stalled broken) do
    @mode mode
    test "native CLI #{@mode}" do
      beam = :code.which(Bash) |> to_string() |> Path.dirname()
      cli = System.get_env("TINYAGENT_ELIXIR_CLI", Path.expand("../cli.ex", __DIR__))

      deps =
        Enum.map(
          [:mint, :jason, :hpax, :jsv, :abnf_parsec, :texture, :idna, :nimble_parsec],
          &Application.app_dir(&1, "ebin")
        )

      script = Path.join(__DIR__, "cli_native.py")

      {output, status} =
        System.cmd("python3", [script, @mode, beam, cli | deps], stderr_to_stdout: true)

      assert status == 0, output
      assert output == @mode <> " passed\n"
    end
  end
end
