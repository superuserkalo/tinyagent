defmodule TinyAgent.Agent do
  @moduledoc "Immutable configuration for synchronous runs with independent history and cancellation."
  alias TinyAgent.{
    AssistantMessage,
    CancellationToken,
    Cancelled,
    ProviderError,
    ProviderRequest,
    ProviderTurn,
    RunEvents,
    RunResult,
    Tool,
    ToolResultMessage,
    UserMessage
  }

  @enforce_keys [:provider, :model, :instructions]
  defstruct [:provider, :model, :instructions, tools: %{}, definitions: []]

  def new(provider, model, options \\ []) do
    options = Keyword.validate!(options, instructions: "", tools: [])

    unless is_struct(provider) and function_exported?(provider.__struct__, :stream, 3),
      do: raise(ArgumentError, "Provider must expose stream/3.")

    text!(model, "Model", true)
    text!(options[:instructions], "Instructions")

    tools =
      Enum.reduce(options[:tools], %{}, fn %Tool{} = tool, tools ->
        name = tool.definition.name
        if Map.has_key?(tools, name), do: raise(ArgumentError, "Tool names must be unique.")
        Map.put(tools, name, tool)
      end)

    %__MODULE__{
      provider: provider,
      model: model,
      instructions: options[:instructions],
      tools: tools,
      definitions: Enum.map(options[:tools], & &1.definition)
    }
  end

  def run(%__MODULE__{} = agent, prompt, options \\ []) do
    options =
      Keyword.validate!(options,
        history: [],
        on_event: nil,
        cancellation: CancellationToken.new(),
        run_id: nil
      )

    text!(prompt, "Prompt")

    id =
      case options[:run_id] do
        nil -> "r_" <> Base.encode16(:crypto.strong_rand_bytes(12), case: :lower)
        id -> id
      end

    text!(id, "Run ID", true)
    callback = options[:on_event]

    unless callback == nil or is_function(callback, 2),
      do: raise(ArgumentError, "on_event must accept an event and cancellation.")

    parent = options[:cancellation]

    unless is_struct(parent, CancellationToken),
      do: raise(ArgumentError, "Expected cancellation token.")

    request =
      ProviderRequest.new(agent.model, agent.instructions, options[:history], agent.definitions)

    request = %{request | history: request.history ++ [UserMessage.new(prompt)]}
    scope = CancellationToken.child(parent)
    delivery = CancellationToken.child(parent)
    events = RunEvents.open(id, callback)

    try do
      outcome =
        try do
          RunEvents.emit(events, :start, scope)
          CancellationToken.check!(scope)
          {:ok, cycle(agent, request, events, scope)}
        catch
          kind, reason -> {:error, RunEvents.failure(events) || {kind, reason, __STACKTRACE__}}
        end

      cancelled = CancellationToken.cancelled?(scope)
      CancellationToken.cancel(scope)

      outcome =
        if cancelled and RunEvents.failure(events) == nil,
          do: {:error, {:error, %Cancelled{}, []}},
          else: outcome

      finish(outcome, events, delivery)
    after
      CancellationToken.cancel(scope)
      CancellationToken.cancel(delivery)
      RunEvents.close(events)
    end
  end

  defp emit_text(events, text, token, scope) do
    RunEvents.check!(events)
    CancellationToken.check!(scope)
    CancellationToken.check!(token)
    text!(text, "Provider text delta")
    if text != "", do: RunEvents.emit(events, {:text_delta, text}, token)
    CancellationToken.check!(scope)
    CancellationToken.check!(token)
  rescue
    error in Cancelled ->
      if error.reason == :aborted, do: CancellationToken.cancel(scope)
      reraise(error, __STACKTRACE__)
  end

  defp cycle(agent, request, events, scope) do
    CancellationToken.check!(scope)
    request_scope = CancellationToken.child(scope)
    sequence = RunEvents.sequence(events)
    provider = agent.provider

    turn =
      try do
        turn =
          provider.__struct__.stream(provider, request,
            cancellation: request_scope,
            on_text: fn text, token ->
              # Retained callbacks cannot cancel a later request or handler.
              RunEvents.check!(events)
              CancellationToken.check!(request_scope)
              emit_text(events, text, token, scope)
            end
          )

        CancellationToken.check!(request_scope)
        turn
      after
        if CancellationToken.cancelled?(request_scope), do: CancellationToken.cancel(scope)
        CancellationToken.cancel(request_scope)
      end

    RunEvents.check!(events)
    CancellationToken.check!(scope)

    unless match?(%ProviderTurn{assistant: %AssistantMessage{}}, turn),
      do: raise(ProviderError, message: "Provider did not return a completed turn.")

    text!(turn.text, "Provider final text")

    for call <- turn.tool_calls do
      unless Map.has_key?(agent.tools, call.name),
        do: raise(ProviderError, message: "Provider requested an unregistered tool: #{call.name}")
    end

    if RunEvents.sequence(events) == sequence and turn.text != "",
      do: RunEvents.emit(events, {:text_delta, turn.text}, scope)

    CancellationToken.check!(scope)
    history = request.history ++ [turn.assistant]

    if turn.tool_calls == [] do
      RunResult.new(turn.text, history, turn.output)
    else
      results = Enum.map(turn.tool_calls, &execute_tool(agent.tools[&1.name], &1, events, scope))
      cycle(agent, %{request | history: history ++ results}, events, scope)
    end
  end

  defp execute_tool(tool, call, events, scope) do
    CancellationToken.check!(scope)
    RunEvents.emit(events, {:tool_start, call.call_id, call.name, call.arguments}, scope)

    outcome =
      try do
        output = Tool.call(tool, call.arguments, scope)
        CancellationToken.check!(scope)
        text!(output, "Tool output")

        if byte_size(output) > 131_072,
          do: raise(ArgumentError, "Tool output exceeds 131072 UTF-8 bytes.")

        {:ok, output, false}
      rescue
        error in Cancelled ->
          {:raised, :error, error, __STACKTRACE__}

        error ->
          detail = Exception.message(error)

          detail =
            if String.valid?(detail),
              do: detail |> String.to_charlist() |> Enum.take(2000) |> List.to_string(),
              else: "Handler failed"

          {:ok, "Tool failed: " <> detail, true}
      catch
        kind, reason -> {:raised, kind, reason, __STACKTRACE__}
      end

    outcome =
      if CancellationToken.cancelled?(scope),
        do: {:raised, :error, %Cancelled{}, []},
        else: outcome

    {output, failed} =
      case outcome do
        {:ok, output, failed} -> {output, failed}
        {:raised, _, _, _} -> {"Tool cancelled.", true}
      end

    RunEvents.emit(events, {:tool_end, call.call_id, call.name, output, failed}, scope)
    CancellationToken.check!(scope)

    case outcome do
      {:ok, _, _} -> ToolResultMessage.new(call.call_id, call.name, output, failed)
      {:raised, kind, reason, trace} -> :erlang.raise(kind, reason, trace)
    end
  end

  defp finish({:ok, result}, events, token) do
    RunEvents.emit(events, {:completed, result.text}, token)
    CancellationToken.check!(token)
    result
  end

  defp finish({:error, {kind, reason, trace}}, events, token) do
    if RunEvents.failure(events) == nil do
      terminal =
        if is_struct(reason, Cancelled) do
          :aborted
        else
          message =
            if kind == :error,
              do: Exception.message(Exception.normalize(kind, reason, trace)),
              else: "Provider #{kind}: #{inspect(reason)}"

          message = if String.valid?(message), do: message, else: "Provider request failed."
          {:failed, message}
        end

      RunEvents.emit(events, terminal, token)
    end

    :erlang.raise(kind, reason, trace)
  end

  defp text!(text, name, nonempty \\ false) do
    unless is_binary(text) and String.valid?(text) and (not nonempty or text != ""),
      do:
        raise(
          ArgumentError,
          "#{name} must be #{if nonempty, do: "nonempty ", else: ""}UTF-8 text."
        )

    text
  end
end
