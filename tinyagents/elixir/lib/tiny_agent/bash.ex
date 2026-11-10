defmodule TinyAgent.Bash do
  @moduledoc "Fresh Bash commands with separate, bounded output and cooperative cancellation."
  alias TinyAgent.{CancellationToken, ProviderError}
  @output_bytes 131_072
  @denied_words MapSet.new(~w[
    rm rmdir unlink shred truncate dd mkfs wipefs fdisk parted
    sudo doas shutdown reboot poweroff halt kill pkill killall
  ])

  def max_output_bytes, do: @output_bytes

  def denied_reason(command) when is_binary(command) do
    normalized = command |> String.downcase() |> String.replace(~r/[\\"']/, "")

    words =
      ~r/[a-z0-9_.-]+/
      |> Regex.scan(normalized)
      |> List.flatten()
      |> MapSet.new()

    blocked =
      words
      |> Enum.sort()
      |> Enum.find(&(MapSet.member?(@denied_words, &1) or String.starts_with?(&1, "mkfs.")))

    cond do
      blocked ->
        "'#{String.slice(blocked, 0, 80)}' is denied"

      MapSet.member?(words, "find") and MapSet.member?(words, "-delete") ->
        "'find -delete' is denied"

      MapSet.member?(words, "git") and
          (MapSet.member?(words, "clean") or MapSet.member?(words, "restore") or
             (MapSet.member?(words, "reset") and MapSet.member?(words, "--hard")) or
             (MapSet.member?(words, "checkout") and MapSet.member?(words, "--"))) ->
        "destructive git command is denied"

      normalized |> String.replace(~r/\s/, "") |> String.contains?(":(){:|:&};:") ->
        "fork bomb is denied"

      true ->
        nil
    end
  end

  def run(command, options \\ []) do
    options = Keyword.validate!(options, cancellation: CancellationToken.new())
    token = options[:cancellation]
    CancellationToken.check!(token)

    unless is_binary(command) and String.valid?(command) and String.trim(command) != "" and
             not String.contains?(command, <<0>>),
           do: raise(ArgumentError, "Bash requires nonempty UTF-8 text without NUL bytes.")

    case denied_reason(command) do
      nil -> execute(command, token)
      reason -> "exit_code: 126\nstdout:\n\nstderr:\nblocked: #{reason}\n"
    end
  end

  defp execute(command, token) do
    executable =
      __MODULE__
      |> :code.which()
      |> to_string()
      |> Path.dirname()
      |> Path.join("../priv/tiny_agent_bash")
      |> Path.expand()

    unless File.regular?(executable),
      do: raise(ProviderError, message: "Bash requires the native macOS or Linux helper.")

    CancellationToken.check!(token)

    port =
      Port.open({:spawn_executable, executable}, [
        :binary,
        :exit_status,
        :use_stdio,
        {:packet, 4},
        {:args, [command]},
        {:env, Enum.map(~w(BASH_ENV ENV SHELLOPTS BASHOPTS), &{String.to_charlist(&1), false})}
      ])

    try do
      {stdout, stderr, status, error} = collect(port, token, {nil, nil, nil, nil})
      CancellationToken.check!(token)
      if error, do: raise(ProviderError, message: error)

      unless is_binary(stdout) and is_binary(stderr) and is_integer(status),
        do: raise(ProviderError, message: "Bash helper did not return a complete observation.")

      observation = "exit_code: #{status}\nstdout:\n#{utf8(stdout)}stderr:\n#{utf8(stderr)}"

      if byte_size(observation) > @output_bytes,
        do: raise(ProviderError, message: "Bash output exceeded #{@output_bytes} UTF-8 bytes.")

      observation
    after
      stop(port)
    end
  end

  defp collect(port, token, {stdout, stderr, status, error} = state) do
    CancellationToken.check!(token)

    receive do
      {^port, {:data, <<1, bytes::binary>>}}
      when stdout == nil and byte_size(bytes) <= @output_bytes ->
        collect(port, token, {bytes, stderr, status, error})

      {^port, {:data, <<2, bytes::binary>>}}
      when stderr == nil and byte_size(bytes) <= @output_bytes ->
        collect(port, token, {stdout, bytes, status, error})

      {^port, {:data, <<3, code::unsigned-big-32>>}} when status == nil ->
        collect(port, token, {stdout, stderr, code, error})

      {^port, {:data, <<4, message::binary>>}} when error == nil and byte_size(message) <= 1024 ->
        collect(port, token, {stdout, stderr, status, utf8(message)})

      {^port, {:exit_status, 0}} ->
        state

      {^port, {:exit_status, code}} ->
        raise ProviderError, message: "Bash helper failed: #{code}"

      {^port, {:data, _bytes}} ->
        raise ProviderError, message: "Invalid Bash helper response."
    after
      10 -> collect(port, token, state)
    end
  end

  # Keep the port open until the helper has killed and joined its child. Closing
  # stdin also triggers that cleanup if the owning Elixir process itself exits.
  defp stop(port) do
    if Port.info(port) != nil do
      try do
        Port.command(port, "c")
      rescue
        ArgumentError -> :ok
      end

      await_exit(port)
    end

    flush(port)
  end

  defp await_exit(port) do
    receive do
      {^port, {:exit_status, _status}} -> :ok
      {^port, {:data, _bytes}} -> await_exit(port)
    after
      10 -> if Port.info(port) != nil, do: await_exit(port)
    end
  end

  defp flush(port) do
    receive do
      {^port, _message} -> flush(port)
      {:EXIT, ^port, :normal} -> flush(port)
    after
      0 -> :ok
    end
  end

  defp utf8(bytes),
    do:
      bytes
      |> String.chunk(:valid)
      |> Enum.map_join(fn
        part -> if String.valid?(part), do: part, else: String.duplicate("�", byte_size(part))
      end)
end
