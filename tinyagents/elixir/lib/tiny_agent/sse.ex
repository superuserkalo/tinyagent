defmodule TinyAgent.SSE do
  @moduledoc false
  alias TinyAgent.{JSON, ProviderError}
  defstruct line: "", data: [], bytes: 0

  def feed(parser, "", state, _consume), do: {:cont, parser, state}

  def feed(parser, bytes, state, consume) do
    case :binary.match(bytes, "\n") do
      :nomatch ->
        {:cont, append_line(parser, bytes), state}

      {index, 1} ->
        line = binary_part(bytes, 0, index)
        rest = binary_part(bytes, index + 1, byte_size(bytes) - index - 1)

        case read_line(append_line(parser, line), state, consume) do
          {:cont, parser, state} -> feed(parser, rest, state, consume)
          {:halt, parser, result} -> {:halt, parser, result}
        end
    end
  end

  def finish(parser, state, consume) do
    case read_line(parser, state, consume) do
      {:cont, parser, state} -> dispatch(parser, state, consume)
      halted -> halted
    end
  end

  defp append_line(parser, bytes) do
    if byte_size(bytes) > JSON.max_bytes() - byte_size(parser.line),
      do: raise(ProviderError, message: "SSE line exceeds its byte limit.")

    %{parser | line: parser.line <> bytes}
  end

  defp read_line(parser, state, consume) do
    unless String.valid?(parser.line),
      do: raise(ProviderError, message: "Provider stream is not valid UTF-8.")

    line =
      if String.ends_with?(parser.line, "\r"),
        do: binary_part(parser.line, 0, byte_size(parser.line) - 1),
        else: parser.line

    parser = %{parser | line: ""}

    case line do
      "" ->
        dispatch(parser, state, consume)

      "data" ->
        {:cont, append_data(parser, ""), state}

      "data:" <> text ->
        text =
          case text do
            " " <> rest -> rest
            _ -> text
          end

        {:cont, append_data(parser, text), state}

      _ ->
        {:cont, parser, state}
    end
  end

  defp append_data(parser, text) do
    added = byte_size(text) + if(parser.data == [], do: 0, else: 1)

    if added > JSON.max_bytes() - parser.bytes,
      do: raise(ProviderError, message: "SSE event exceeds its byte limit.")

    %{parser | data: [text | parser.data], bytes: parser.bytes + added}
  end

  defp dispatch(%{data: []} = parser, state, _consume), do: {:cont, parser, state}

  defp dispatch(parser, state, consume) do
    source = parser.data |> Enum.reverse() |> Enum.join("\n")

    if source == "[DONE]",
      do: raise(ProviderError, message: "Provider stream ended without a complete turn.")

    parser = %{parser | data: [], bytes: 0}

    case consume.(JSON.parse_object!(source), state) do
      {:cont, state} -> {:cont, parser, state}
      {:halt, result} -> {:halt, parser, result}
    end
  end
end
