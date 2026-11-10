defmodule TinyAgent.AnthropicProvider do
  @behaviour TinyAgent.Provider
  alias TinyAgent.{
    AssistantMessage,
    HTTP,
    JSON,
    ProviderRequest,
    ProviderStream,
    ToolResultMessage,
    UserMessage
  }

  @enforce_keys [:http, :max_tokens]
  defstruct [:api_key, :http, :max_tokens]

  def new(options \\ []) do
    options =
      Keyword.validate!(options,
        api_key: nil,
        endpoint: "https://api.anthropic.com/v1/messages",
        timeout: 180_000,
        max_tokens: 8192,
        connect: &Mint.HTTP.connect/4
      )

    unless is_integer(options[:max_tokens]) and options[:max_tokens] > 0,
      do: raise(ArgumentError, "max_tokens must be positive.")

    %__MODULE__{
      api_key: options[:api_key],
      max_tokens: options[:max_tokens],
      http: HTTP.new(options[:endpoint], options[:timeout], options[:connect])
    }
  end

  @impl true
  def stream(%__MODULE__{} = provider, %ProviderRequest{} = request, options \\ []) do
    key = ProviderStream.key!(provider.api_key, "ANTHROPIC_API_KEY", options)
    messages = request.history |> Enum.reduce([], &serialize/2) |> Enum.reverse()

    payload = %{
      "model" => request.model,
      "messages" => messages,
      "max_tokens" => provider.max_tokens,
      "stream" => true
    }

    payload =
      if request.instructions == "",
        do: payload,
        else: Map.put(payload, "system", request.instructions)

    payload =
      if request.tools == [] do
        payload
      else
        Map.put(
          payload,
          "tools",
          Enum.map(request.tools, fn tool ->
            %{
              "name" => tool.name,
              "description" => tool.description,
              "input_schema" => tool.parameters
            }
          end)
        )
      end

    headers = [{"x-api-key", key}, {"anthropic-version", "2023-06-01"}]
    ProviderStream.run(provider.http, headers, payload, TinyAgent.AnthropicResponse, options)
  end

  defp serialize(%UserMessage{text: text}, messages),
    do: [%{"role" => "user", "content" => text} | messages]

  defp serialize(%AssistantMessage{} = message, messages) do
    content =
      case message.replay do
        [replay | _] ->
          replay
          |> JSON.object!("Anthropic replay")
          |> Map.get("content")
          |> JSON.list!("replay content")

        [] ->
          text =
            if message.text == "", do: [], else: [%{"type" => "text", "text" => message.text}]

          text ++
            Enum.map(message.tool_calls, fn call ->
              %{
                "type" => "tool_use",
                "id" => call.call_id,
                "name" => call.name,
                "input" => call.arguments
              }
            end)
      end

    [%{"role" => "assistant", "content" => content} | messages]
  end

  defp serialize(%ToolResultMessage{} = result, messages) do
    block = %{
      "type" => "tool_result",
      "tool_use_id" => result.call_id,
      "content" => result.output,
      "is_error" => result.is_error
    }

    case messages do
      [%{"role" => "user", "content" => content} = previous | rest] when is_list(content) ->
        if Enum.all?(content, &match?(%{"type" => "tool_result"}, &1)),
          do: [Map.put(previous, "content", content ++ [block]) | rest],
          else: [%{"role" => "user", "content" => [block]} | messages]

      _ ->
        [%{"role" => "user", "content" => [block]} | messages]
    end
  end
end

defmodule TinyAgent.AnthropicResponse do
  @moduledoc false
  alias TinyAgent.{JSON, ProviderError, ProviderTurn, ToolCall}
  def init, do: %{message: nil, blocks: %{}, usage: %{}}

  def consume(event, state, on_text, cancellation) do
    case JSON.string!(event, "type", true) do
      "message_start" ->
        if state.message != nil,
          do: raise(ProviderError, message: "Repeated Anthropic message_start.")

        message = JSON.object!(event["message"], "message")

        usage =
          if Map.has_key?(message, "usage"),
            do: JSON.object!(message["usage"], "usage"),
            else: %{}

        {:cont, %{state | message: message, usage: usage}}

      "content_block_start" ->
        index = JSON.index!(event["index"])

        if state.message == nil or Map.has_key?(state.blocks, index),
          do: raise(ProviderError, message: "Invalid Anthropic block order.")

        item = JSON.object!(event["content_block"], "content block")
        JSON.string!(item, "type", true)

        {:cont,
         put_in(state.blocks[index], %{value: item, arguments: "", fields: %{}, open: true})}

      "content_block_delta" ->
        index = JSON.index!(event["index"])
        block = open_block!(state, index)
        delta = JSON.object!(event["delta"], "block delta")
        block = append_delta(block, delta, on_text, cancellation)
        {:cont, put_in(state.blocks[index], block)}

      "content_block_stop" ->
        index = JSON.index!(event["index"])
        block = open_block!(state, index)

        item =
          Enum.reduce(block.fields, block.value, fn {field, fragments}, item ->
            previous = if Map.has_key?(item, field), do: JSON.string!(item, field), else: ""
            Map.put(item, field, IO.iodata_to_binary([previous | Enum.reverse(fragments)]))
          end)

        item =
          if JSON.string!(item, "type", true) == "tool_use" do
            input =
              if block.arguments == "",
                do: JSON.object!(item["input"], "tool arguments"),
                else: JSON.parse_object!(block.arguments)

            Map.put(item, "input", input)
          else
            item
          end

        {:cont, put_in(state.blocks[index], %{block | value: item, open: false, fields: %{}})}

      "message_delta" ->
        if state.message == nil,
          do: raise(ProviderError, message: "Anthropic delta preceded message_start.")

        message = Map.merge(state.message, JSON.object!(event["delta"], "message delta"))

        usage =
          if Map.has_key?(event, "usage"),
            do: Map.merge(state.usage, JSON.object!(event["usage"], "usage delta")),
            else: state.usage

        {:cont, %{state | message: message, usage: usage}}

      "message_stop" ->
        {:halt, complete(state)}

      "error" ->
        detail = event["error"] |> JSON.object!("stream error") |> JSON.string!("message")
        raise ProviderError, message: detail

      _ ->
        {:cont, state}
    end
  end

  defp open_block!(state, index) do
    case Map.get(state.blocks, index) do
      %{open: true} = block -> block
      _ -> raise ProviderError, message: "Anthropic event referenced an unopened block."
    end
  end

  defp append_delta(block, delta, on_text, cancellation) do
    kind = JSON.string!(delta, "type", true)

    fields =
      case kind do
        "text_delta" -> {"text", "text"}
        "thinking_delta" -> {"thinking", "thinking"}
        "signature_delta" -> {"thinking", "signature"}
        "input_json_delta" -> {"tool_use", "partial_json"}
        _ -> nil
      end

    if fields == nil do
      block
    else
      {expected, field} = fields

      if JSON.string!(block.value, "type", true) != expected,
        do: raise(ProviderError, message: "Anthropic delta does not match its block.")

      addition = JSON.string!(delta, field)

      if kind == "input_json_delta" do
        %{block | arguments: JSON.append_arguments!(block.arguments, addition)}
      else
        if kind == "text_delta" and addition != "" and on_text != nil,
          do: on_text.(addition, cancellation)

        %{block | fields: Map.update(block.fields, field, [addition], &[addition | &1])}
      end
    end
  end

  defp complete(state) do
    if state.message == nil,
      do: raise(ProviderError, message: "Anthropic stream ended before a complete message.")

    reason = JSON.string!(state.message, "stop_reason", true)

    unless reason in ["end_turn", "tool_use", "stop_sequence", "refusal"],
      do: raise(ProviderError, message: "Anthropic response is incomplete: #{reason}")

    {text, calls, output, _ids} =
      state.blocks
      |> Enum.sort_by(&elem(&1, 0))
      |> Enum.reduce({[], [], [], MapSet.new()}, fn {_index, block}, {text, calls, output, ids} ->
        if block.open,
          do:
            raise(ProviderError,
              message: "Anthropic stream ended with an unfinished content block."
            )

        item = block.value

        case JSON.string!(item, "type", true) do
          "tool_use" ->
            id = JSON.string!(item, "id", true)
            name = JSON.string!(item, "name", true)

            if MapSet.member?(ids, id),
              do: raise(ProviderError, message: "Duplicate Anthropic tool call.")

            arguments = JSON.object!(item["input"], "tool arguments")
            JSON.append_arguments!("", JSON.encode!(arguments))

            {text, [ToolCall.new(id, name, arguments) | calls], [item | output],
             MapSet.put(ids, id)}

          "text" ->
            {[JSON.string!(item, "text") | text], calls, [item | output], ids}

          _ ->
            {text, calls, [item | output], ids}
        end
      end)

    output = Enum.reverse(output)
    message = state.message |> Map.put("content", output) |> Map.put("usage", state.usage)

    ProviderTurn.new(
      text |> Enum.reverse() |> IO.iodata_to_binary(),
      Enum.reverse(calls),
      [message],
      output
    )
  end
end
