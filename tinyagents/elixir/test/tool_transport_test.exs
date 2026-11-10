defmodule TinyAgent.ToolTransportTest do
  use ExUnit.Case, async: true
  import TinyAgent.CoreSupport
  import TinyAgent.HttpSupport

  alias TinyAgent.{
    Agent,
    CancellationToken,
    HttpServer,
    ProviderTurn,
    RpcSession,
    Tool,
    ToolCall,
    ToolResultMessage
  }

  defp agent(owner) do
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
            send(owner, :tool_waiting)

            try do
              wait_until(fn -> CancellationToken.cancelled?(token) end)
              CancellationToken.check!(token)
            after
              send(owner, :tool_cleaned)
            end
          end

          arguments["text"]
        end
      )

    provider =
      scripted(fn request, _options ->
        case List.last(request.history) do
          %ToolResultMessage{output: output} ->
            ProviderTurn.new(output, [], [], [])

          %{text: text} ->
            send(owner, {:history, length(request.history)})
            arguments = %{"text" => if(text == "invalid", do: "", else: text)}
            ProviderTurn.new("", [ToolCall.new("call", "echo", arguments)], [], [])
        end
      end)

    Agent.new(provider, "fixture", tools: [tool])
  end

  test "native RPC subprocess tools, recovery, cancellation and rollback" do
    beam = :code.which(TinyAgent.Rpc) |> to_string() |> Path.dirname()

    dependencies =
      Enum.map(
        [:jason, :jsv, :abnf_parsec, :texture, :idna, :nimble_parsec],
        &Application.app_dir(&1, "ebin")
      )

    {output, status} =
      System.cmd("python3", [Path.join(__DIR__, "tool_rpc_native.py"), beam | dependencies],
        stderr_to_stdout: true
      )

    assert status == 0, output
    assert output == "tools passed\n"
  end

  test "tool history commits only after successful terminal delivery" do
    owner = self()

    rpc =
      RpcSession.new(agent(owner), fn value, _token ->
        if value["type"] == "agent_end" and value["text"] == "failed delivery",
          do: raise("disconnect")

        send(owner, {:rpc_event, value})
      end)

    try do
      for {text, count} <- [{"seed", 4}, {"failed delivery", 4}, {"next", 8}] do
        RpcSession.handle(rpc, %{"type" => "prompt", "message" => text})
        RpcSession.wait_idle(rpc)
        assert RpcSession.get_state(rpc)["messageCount"] == count
      end

      assert_receive {:rpc_event, %{"command" => "prompt", "success" => true}}
      assert_receive {:history, 1}
      assert_receive {:history, 5}
      assert_receive {:history, 5}
    after
      RpcSession.close(rpc)
    end
  end

  @tag :tcp
  test "HTTP tools and cancellation preserve prior completed history" do
    {:ok, server} = HttpServer.start_link(agent(self()), port: 0)

    try do
      url = URI.parse(HttpServer.url(server))
      id = create(url)
      path = "/sessions/#{id}"

      for text <- ["café", "invalid"] do
        values = events(request(url, "POST", path <> "/prompts", %{"message" => text}))

        assert Enum.map(values, & &1["type"]) == [
                 "agent_start",
                 "tool_execution_start",
                 "tool_execution_end",
                 "text_delta",
                 "agent_end"
               ]

        assert Enum.map(values, & &1["sequence"]) == [1, 2, 3, 4, 5]
        assert length(Enum.uniq(Enum.map(values, & &1["runId"]))) == 1
        assert Enum.at(values, 2)["isError"] == (text == "invalid")
        assert List.last(values)["text"] == Enum.at(values, 2)["output"]
      end

      assert_receive {:history, 1}
      assert_receive {:history, 5}
      pending = open(url, "POST", path <> "/prompts", %{"message" => "wait"})
      assert_receive :tool_waiting, 1000
      assert %{status: 200} = request(url, "POST", path <> "/cancel")
      values = events(response(pending))

      assert Enum.map(values, & &1["type"]) == [
               "agent_start",
               "tool_execution_start",
               "tool_execution_end",
               "agent_end"
             ]

      assert Enum.at(values, 2)["isError"]
      assert List.last(values)["status"] == "aborted"
      assert_receive :tool_cleaned
      assert_receive {:history, 9}

      assert List.last(events(request(url, "POST", path <> "/prompts", %{"message" => "next"})))[
               "status"
             ] == "completed"

      assert_receive {:history, 9}
    after
      HttpServer.close(server)
    end
  end
end
