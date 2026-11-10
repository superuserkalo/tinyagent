for path <- System.argv(), do: Code.prepend_path(path)
Application.ensure_all_started(:crypto)

defmodule TinyAgent.ToolRpcProvider do
  defstruct []
  alias TinyAgent.{ProviderTurn, ToolCall, ToolResultMessage}

  def stream(_provider, request, _options) do
    case List.last(request.history) do
      %ToolResultMessage{output: output} ->
        ProviderTurn.new(output, [], [], [])

      %{text: "history"} ->
        ProviderTurn.new("messages=#{length(request.history)}", [], [], [])

      %{text: text} ->
        arguments = %{"text" => if(text == "invalid", do: "", else: text)}
        ProviderTurn.new("", [ToolCall.new("call", "echo", arguments)], [], [])
    end
  end
end

defmodule TinyAgent.ToolRpcFixture do
  alias TinyAgent.{Agent, CancellationToken, Rpc, Tool}

  def run do
    tool =
      Tool.new(
        "echo",
        "",
        %{
          "type" => "object",
          "properties" => %{"text" => %{"type" => "string", "minLength" => 1}},
          "required" => ["text"]
        },
        fn arguments, token ->
          if arguments["text"] == "wait" do
            IO.puts(:stderr, "tool-waiting")

            try do
              wait(token)
            after
              IO.puts(:stderr, "tool-cleaned")
            end
          end

          arguments["text"]
        end
      )

    Rpc.run_stdio(Agent.new(%TinyAgent.ToolRpcProvider{}, "fixture", tools: [tool]))
  end

  defp wait(token) do
    CancellationToken.check!(token)
    Process.sleep(5)
    wait(token)
  end
end

TinyAgent.ToolRpcFixture.run()
