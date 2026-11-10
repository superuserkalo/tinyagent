defmodule TinyAgent.OpenAIProvider do
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

  @enforce_keys [:http]
  defstruct [:api_key, :http]

  def new(options \\ []) do
    options =
      Keyword.validate!(options,
        api_key: nil,
        endpoint: "https://api.openai.com/v1/responses",
        timeout: 180_000,
        connect: &Mint.HTTP.connect/4
      )

    %__MODULE__{
      api_key: options[:api_key],
      http: HTTP.new(options[:endpoint], options[:timeout], options[:connect])
    }
  end

  @impl true
  def stream(%__MODULE__{} = provider, %ProviderRequest{} = request, options \\ []) do
    key = ProviderStream.key!(provider.api_key, "OPENAI_API_KEY", options)
    input = Enum.flat_map(request.history, &serialize/1)

    tools =
      Enum.map(request.tools, fn tool ->
        %{
          "type" => "function",
          "name" => tool.name,
          "description" => tool.description,
          "parameters" => tool.parameters,
          "strict" => true
        }
      end)

    payload = %{
      "model" => request.model,
      "instructions" => request.instructions,
      "input" => input,
      "tools" => tools,
      "parallel_tool_calls" => false,
      "store" => false,
      "include" => ["reasoning.encrypted_content"],
      "stream" => true
    }

    ProviderStream.run(
      provider.http,
      [{"authorization", "Bearer #{key}"}],
      payload,
      TinyAgent.OpenAIResponse,
      options
    )
  end

  defp serialize(%UserMessage{text: text}), do: [%{"role" => "user", "content" => text}]
  defp serialize(%AssistantMessage{replay: [_ | _] = replay}), do: replay

  defp serialize(%AssistantMessage{text: text, tool_calls: calls}) do
    message = if text == "", do: [], else: [%{"role" => "assistant", "content" => text}]

    message ++
      Enum.map(calls, fn call ->
        %{
          "type" => "function_call",
          "call_id" => call.call_id,
          "name" => call.name,
          "arguments" => JSON.encode!(call.arguments)
        }
      end)
  end

  defp serialize(%ToolResultMessage{call_id: id, output: output}),
    do: [%{"type" => "function_call_output", "call_id" => id, "output" => output}]
end

defmodule TinyAgent.OpenAIResponse do
  @moduledoc false
  alias TinyAgent.{JSON, ProviderError, ProviderTurn, ToolCall}
  def init, do: %{output: %{}, arguments: %{}}

  def consume(event, state, on_text, cancellation) do
    type = JSON.string!(event, "type", true)

    case type do
      type when type in ["response.output_item.added", "response.output_item.done"] ->
        index = JSON.index!(event["output_index"])
        item = JSON.object!(event["item"], "output item")

        state =
          if JSON.string!(item, "type", true) == "function_call" do
            raw = JSON.string!(item, "arguments")

            if raw == "",
              do: state,
              else: put_in(state.arguments[index], JSON.append_arguments!("", raw))
          else
            state
          end

        {:cont, put_in(state.output[index], item)}

      "response.output_text.delta" ->
        delta = JSON.string!(event, "delta")
        if delta != "" and on_text != nil, do: on_text.(delta, cancellation)
        {:cont, state}

      "response.function_call_arguments.delta" ->
        index = JSON.index!(event["output_index"])

        raw =
          JSON.append_arguments!(
            Map.get(state.arguments, index, ""),
            JSON.string!(event, "delta")
          )

        {:cont, put_in(state.arguments[index], raw)}

      "response.function_call_arguments.done" ->
        index = JSON.index!(event["output_index"])

        {:cont,
         put_in(
           state.arguments[index],
           JSON.append_arguments!("", JSON.string!(event, "arguments"))
         )}

      "response.completed" ->
        response = JSON.object!(event["response"], "completed response")

        if Map.has_key?(response, "status") and JSON.string!(response, "status") != "completed",
          do: raise(ProviderError, message: "OpenAI response is incomplete.")

        state =
          if Map.has_key?(response, "output") do
            output =
              response["output"]
              |> JSON.list!("output")
              |> Enum.with_index()
              |> Map.new(fn {item, index} -> {index, JSON.object!(item, "output item")} end)

            %{state | output: output}
          else
            state
          end

        {:halt, complete(state)}

      type when type in ["response.failed", "response.incomplete", "error"] ->
        raise ProviderError, message: "OpenAI stream failed: #{type}"

      _ ->
        {:cont, state}
    end
  end

  defp complete(state) do
    {text, calls, output, _ids} =
      state.output
      |> Enum.sort_by(&elem(&1, 0))
      |> Enum.reduce({[], [], [], MapSet.new()}, fn {index, item}, {text, calls, output, ids} ->
        case JSON.string!(item, "type", true) do
          "function_call" ->
            id = JSON.string!(item, "call_id", true)
            name = JSON.string!(item, "name", true)

            if MapSet.member?(ids, id),
              do: raise(ProviderError, message: "Duplicate OpenAI tool call.")

            raw = JSON.string!(item, "arguments")
            raw = if raw == "", do: Map.get(state.arguments, index, ""), else: raw
            raw = JSON.append_arguments!("", raw)
            call = ToolCall.new(id, name, JSON.parse_object!(raw))

            {text, [call | calls], [Map.put(item, "arguments", raw) | output],
             MapSet.put(ids, id)}

          "message" ->
            parts =
              item["content"]
              |> JSON.list!("message content")
              |> Enum.map(fn part ->
                part = JSON.object!(part, "message content")

                if JSON.string!(part, "type", true) == "output_text",
                  do: JSON.string!(part, "text"),
                  else: ""
              end)

            {[parts | text], calls, [item | output], ids}

          _ ->
            {text, calls, [item | output], ids}
        end
      end)

    output = Enum.reverse(output)

    ProviderTurn.new(
      text |> Enum.reverse() |> IO.iodata_to_binary(),
      Enum.reverse(calls),
      output,
      output
    )
  end
end
