defmodule TinyAgent.RpcInput do
  @moduledoc false
  alias TinyAgent.{CancellationToken, RpcError, RpcIO, RpcOutput, RpcProtocol, RpcSession}

  def run(descriptor, session, output, stopping) do
    case RpcIO.open(descriptor, 0) do
      {:error, message} ->
        raise RpcProtocol.error("input_error", message)

      {:ok, handle} ->
        try do
          read(handle, session, output, stopping, "")
        after
          RpcIO.close(handle)
        end
    end
  end

  defp read(handle, session, output, stopping, line) do
    if CancellationToken.cancelled?(stopping) do
      :ok
    else
      case RpcIO.read(handle) do
        :again ->
          Process.sleep(10)
          read(handle, session, output, stopping, line)

        :eof ->
          if line != "", do: process(line, session, output)

        {:error, message} ->
          raise RpcProtocol.error("input_error", message)

        {:ok, chunk} ->
          case lines(chunk, line, session, output, stopping) do
            :stop -> :ok
            {:continue, line} -> read(handle, session, output, stopping, line)
          end
      end
    end
  end

  defp lines(chunk, line, session, output, stopping) do
    if CancellationToken.cancelled?(stopping) do
      :stop
    else
      {part, rest} =
        case :binary.match(chunk, "\n") do
          :nomatch ->
            {chunk, nil}

          {index, 1} ->
            {binary_part(chunk, 0, index),
             binary_part(chunk, index + 1, byte_size(chunk) - index - 1)}
        end

      if byte_size(line) + byte_size(part) > RpcProtocol.line_bytes() do
        report(
          output,
          RpcProtocol.error(
            "line_too_large",
            "RPC input exceeds #{RpcProtocol.line_bytes()} UTF-8 bytes"
          )
        )

        :stop
      else
        line = line <> part

        cond do
          rest == nil -> {:continue, line}
          process(line, session, output) == :stop -> :stop
          rest == "" -> {:continue, ""}
          true -> lines(rest, "", session, output, stopping)
        end
      end
    end
  end

  defp process(line, session, output) do
    case RpcProtocol.decode(line) do
      {:ok, command} ->
        try do
          RpcSession.handle(session, command)
        rescue
          error in RpcError ->
            if error.code == "output_too_large",
              do: RpcSession.cancel(session),
              else: reraise(error, __STACKTRACE__)
        end

        :continue

      {:error, error, action} ->
        report(output, error)
        action
    end
  end

  defp report(output, error), do: RpcOutput.emit(output, RpcProtocol.error_value(error))
end
