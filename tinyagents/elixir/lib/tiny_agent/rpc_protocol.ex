defmodule TinyAgent.RpcError do
  defexception [:code, :message]
end

defmodule TinyAgent.RpcProtocol do
  @moduledoc false
  alias TinyAgent.RpcError
  @line_bytes 1_048_576
  @queue_bytes 4_194_304

  def line_bytes, do: @line_bytes
  def queue_bytes, do: @queue_bytes

  def error(code, message), do: %RpcError{code: code, message: message}

  def error_value(%RpcError{} = error),
    do: %{
      "type" => "protocol_error",
      "error" => %{"code" => error.code, "message" => error.message}
    }

  def frame(value) do
    bytes = Jason.encode!(value)

    if byte_size(bytes) > @line_bytes do
      error = error("output_too_large", "RPC output exceeds #{@line_bytes} UTF-8 bytes")
      {Jason.encode!(error_value(error)) <> "\n", error}
    else
      {bytes <> "\n", nil}
    end
  end

  def decode(bytes) do
    cond do
      byte_size(bytes) > @line_bytes ->
        {:error, error("line_too_large", "RPC input exceeds #{@line_bytes} UTF-8 bytes"), :stop}

      not String.valid?(bytes) ->
        {:error, error("invalid_utf8", "RPC input is not valid UTF-8"), :stop}

      true ->
        case Jason.decode(bytes, strings: :copy) do
          {:ok, command} -> {:ok, command}
          {:error, _} -> {:error, error("invalid_json", "RPC input is not valid JSON"), :continue}
        end
    end
  end
end
