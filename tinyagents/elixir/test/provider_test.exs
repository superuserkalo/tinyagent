defmodule TinyAgent.ProviderTest do
  use ExUnit.Case, async: true
  import TinyAgent.TestSupport

  alias TinyAgent.{
    AnthropicProvider,
    FixtureTransport,
    OpenAIProvider,
    ProviderError,
    ToolCall,
    ToolDefinition,
    ToolResultMessage,
    UserMessage
  }

  for {module, prefix} <- [{OpenAIProvider, "openai"}, {AnthropicProvider, "anthropic"}] do
    test "#{prefix} shared fixtures and opaque replay" do
      shared_fixtures(unquote(module), unquote(prefix))
    end
  end

  defp shared_fixtures(module, prefix) do
    {provider, socket} =
      provider(module, fixture(prefix <> "_tool"), framing: :chunked)

    turn = stream(provider, request())
    assert [%{arguments: %{"text" => "café"}} = call] = turn.tool_calls
    assert FixtureTransport.snapshot(socket).closed
    {headers, payload} = sent(socket)
    assert headers =~ "POST /api?mode=test HTTP/1.1"
    assert payload["stream"] == true

    history = [
      UserMessage.new("go"),
      turn.assistant,
      ToolResultMessage.new(call.call_id, call.name, "café")
    ]

    history =
      if module == AnthropicProvider,
        do: history ++ [ToolResultMessage.new("second", "echo", "failed", true)],
        else: history

    {provider, socket} = provider(module, fixture(prefix <> "_text"))
    caller = self()

    answer =
      stream(provider, request(history),
        on_text: fn text, _token -> send(caller, {:delta, text}) end
      )

    assert answer.text == "done\ncafé"
    assert_receive {:delta, "done\n"}
    assert_receive {:delta, "café"}
    assert FixtureTransport.snapshot(socket).closed
    {_, next} = sent(socket)

    if module == OpenAIProvider do
      assert headers =~ "authorization: Bearer fixture-key"
      assert payload["store"] == false
      assert payload["include"] == ["reasoning.encrypted_content"]
      assert next["include"] == ["reasoning.encrypted_content"]
      assert payload["parallel_tool_calls"] == false
      assert payload["tools"] |> hd() |> get_in(["parameters", "type"]) == "object"
      assert Enum.at(next["input"], 1)["encrypted_content"] == "opaque-fixture"
      assert Jason.decode!(Enum.at(next["input"], 2)["arguments"]) == %{"text" => "café"}
      assert Enum.at(next["input"], 3)["type"] == "function_call_output"
    else
      assert headers =~ "x-api-key: fixture-key"
      assert headers =~ "anthropic-version: 2023-06-01"
      assert hd(turn.replay)["fixture_metadata"] == "keep"
      assert hd(turn.replay)["usage"] == %{"input_tokens" => 8, "output_tokens" => 12}

      assert next["messages"]
             |> Enum.at(1)
             |> get_in(["content"])
             |> hd()
             |> Map.get("signature") == "signed-fixture"

      results = Enum.at(next["messages"], 2)["content"]
      assert length(results) == 2
      assert Enum.at(results, 1)["is_error"] == true
    end
  end

  test "native values retain JSON objects and arrays" do
    call = ToolCall.new("call", "echo", %{nested: %{value: "keep"}, empty: %{}, list: []})
    changed = put_in(call.arguments["nested"]["value"], "changed")
    assert changed.arguments["nested"]["value"] == "changed"
    assert call.arguments == %{"nested" => %{"value" => "keep"}, "empty" => %{}, "list" => []}
    assert ToolDefinition.new("echo", "", false).parameters == false
    assert_raise ProviderError, fn -> ToolCall.new("call", "echo", []) end
  end

  test "SSE framing accepts split UTF-8, multiline JSON and EOF dispatch" do
    complete = ~s({"type":"response.completed","response":{"output":[]}})

    for body <- [
          ": comment\r\nevent: ignored\r\ndata: {\"type\":\"response.completed\",\r\ndata: \"response\":{\"output\":[]}}\r\n\r\n",
          "data: " <> complete,
          "data: " <> complete <> "\n\ndata: malformed trailing bytes\n\n"
        ] do
      {provider, socket} = provider(OpenAIProvider, body)
      assert stream(provider, request()).text == ""
      assert FixtureTransport.snapshot(socket).closed
    end
  end

  test "malformed SSE and bounded lines and events close HTTP" do
    for body <- [
          "data: []\n\n",
          "data: {\"x\":NaN}\n\n",
          "data: {\"x\":1e999}\n\n",
          "data: [DONE]\n\n",
          "data: " <> <<255>> <> "\n\n",
          String.duplicate("x", 1_048_577),
          "data: " <>
            String.duplicate(" ", 600_000) <> "\ndata: " <> String.duplicate(" ", 600_000) <> "\n"
        ] do
      reject(OpenAIProvider, body)
    end
  end

  test "cumulative arguments are bounded for both providers" do
    fragment = String.duplicate(" ", 600_000)

    delta = %{
      "type" => "response.function_call_arguments.delta",
      "output_index" => 0,
      "delta" => fragment
    }

    reject(OpenAIProvider, events([delta, delta]))

    delta = %{
      "type" => "content_block_delta",
      "index" => 0,
      "delta" => %{"type" => "input_json_delta", "partial_json" => fragment}
    }

    reject(
      AnthropicProvider,
      events([
        %{"type" => "message_start", "message" => %{}},
        %{
          "type" => "content_block_start",
          "index" => 0,
          "content_block" => %{"type" => "tool_use", "input" => %{}}
        },
        delta,
        delta
      ])
    )
  end

  test "OpenAI rejects incomplete turns, invalid tool arguments and duplicate calls" do
    call = %{"type" => "function_call", "call_id" => "c", "name" => "echo", "arguments" => "{}"}

    for event <- [
          %{
            "type" => "response.function_call_arguments.delta",
            "output_index" => -1,
            "delta" => "{}"
          },
          %{"type" => "response.output_text.delta", "delta" => 42},
          %{
            "type" => "response.completed",
            "response" => %{"status" => "incomplete", "output" => []}
          },
          %{"type" => "response.completed", "response" => %{"output" => [nil]}},
          %{"type" => "response.completed", "response" => %{"output" => [call, call]}},
          %{
            "type" => "response.completed",
            "response" => %{"output" => [Map.put(call, "arguments", "[]")]}
          },
          %{"type" => "response.failed"},
          %{"type" => "response.output_text.delta", "delta" => "partial"}
        ],
        do: reject(OpenAIProvider, events([event]))

    {provider, _} =
      provider(
        OpenAIProvider,
        events([
          %{"type" => "response.output_item.added", "output_index" => 100, "item" => call},
          %{"type" => "response.completed", "response" => %{}}
        ])
      )

    assert hd(stream(provider, request()).tool_calls).call_id == "c"
  end

  test "Anthropic rejects incomplete, mismatched and unclosed blocks" do
    body = fixture("anthropic_tool")

    for invalid <- [
          events([%{"type" => "message_stop"}]),
          String.replace(body, "\"stop_reason\":\"tool_use\"", "\"stop_reason\":\"max_tokens\""),
          String.replace(body, "\"type\":\"signature_delta\"", "\"type\":\"text_delta\""),
          String.replace(
            body,
            "data: {\"type\":\"content_block_stop\",\"index\":1}",
            ": missing stop"
          ),
          String.replace(body, "\"type\":\"input_json_delta\"", "\"type\":\"thinking_delta\"")
        ],
        do: reject(AnthropicProvider, invalid)
  end

  test "HTTP informational responses precede the final status" do
    {provider, socket} =
      provider(OpenAIProvider, fixture("openai_text"),
        prefix:
          "HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 103 Early Hints\r\nlink: </fixture>\r\n\r\n"
      )

    assert stream(provider, request()).text == "done\ncafé"
    assert FixtureTransport.snapshot(socket).closed
  end

  test "HTTP failures and malformed framing are errors" do
    for status <- [101, 302, 401, 429, 500] do
      {provider, socket} = provider(OpenAIProvider, "", status: status)

      assert_raise ProviderError, "Provider HTTP #{status}.", fn ->
        stream(provider, request())
      end

      assert FixtureTransport.snapshot(socket).closed
    end

    {provider, socket} =
      provider(OpenAIProvider, "", fragments: ["HTTP/1.1 200 OK\r\ncontent-length: nope\r\n\r\n"])

    assert_raise ProviderError, fn -> stream(provider, request()) end
    assert FixtureTransport.snapshot(socket).closed
  end

  defp reject(module, body) do
    {provider, socket} = provider(module, body, fragment_size: 8192)
    assert_raise ProviderError, fn -> stream(provider, request()) end
    assert FixtureTransport.snapshot(socket).closed
  end
end
