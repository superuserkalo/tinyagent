defmodule TinyAgent.Provider do
  @moduledoc "Streaming providers accept a request, an optional text callback, and a cancellation token."
  @callback stream(struct(), struct(), keyword()) :: struct()
end

defmodule TinyAgent.ProviderStream do
  @moduledoc false
  alias TinyAgent.{CancellationToken, Cancelled, HTTP, ProviderError, SSE}

  def key!(configured, name, options) do
    if token = Keyword.get(options, :cancellation), do: CancellationToken.check!(token)
    key = configured || System.get_env(name)

    unless is_binary(key) and String.trim(key) != "",
      do: raise(ProviderError, message: "#{name} is not set.")

    if Regex.match?(~r/[\r\n\x00]/, key), do: raise(ArgumentError, "Invalid API key header.")
    key
  end

  def run(http, headers, payload, parser, options) do
    options = Keyword.validate!(options, on_text: nil, cancellation: CancellationToken.new())
    parent = Keyword.fetch!(options, :cancellation)
    CancellationToken.check!(parent)
    scope = CancellationToken.with_timeout(parent, http.timeout)
    on_text = Keyword.fetch!(options, :on_text)

    unless on_text == nil or is_function(on_text, 2),
      do: raise(ArgumentError, "on_text must accept text and cancellation.")

    consume = fn event, state ->
      result = parser.consume(event, state, on_text, scope)
      CancellationToken.check!(scope)
      result
    end

    try do
      result =
        HTTP.stream(http, headers, payload, scope, {%SSE{}, parser.init()}, fn bytes,
                                                                               {sse, state} ->
          case SSE.feed(sse, bytes, state, consume) do
            {:cont, sse, state} -> {:cont, {sse, state}}
            {:halt, _sse, turn} -> {:halt, turn}
          end
        end)

      CancellationToken.check!(scope)

      case result do
        {:halt, turn} ->
          turn

        {:done, {sse, state}} ->
          case SSE.finish(sse, state, consume) do
            {:halt, _, turn} ->
              turn

            {:cont, _, _} ->
              raise ProviderError, message: "Provider stream ended before a complete turn."
          end
      end
    rescue
      error ->
        CancellationToken.check!(parent)

        case CancellationToken.reason(scope) do
          :timeout ->
            raise ProviderError, message: "Provider request timed out.", cause: error

          :aborted ->
            raise Cancelled

          nil ->
            if is_struct(error, ProviderError), do: reraise(error, __STACKTRACE__)
            raise ProviderError, message: "Provider request failed.", cause: error
        end
    end
  end
end
