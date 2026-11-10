defmodule TinyAgent.CLI do
  @moduledoc false
  alias TinyAgent.{
    Agent,
    Bash,
    CancellationToken,
    Cancelled,
    HttpServer,
    OpenAIProvider,
    ProviderError,
    Rpc,
    RpcIO,
    Tool
  }

  @instructions """
  You are TinyAgent, a coding agent in the current directory.
  Inspect before changing files. Use bash for shell and file work. Each call is a fresh process.
  """

  def agent(provider, model) do
    tool =
      Tool.new(
        "bash",
        "Run a non-interactive bash command in the current directory.",
        %{
          "type" => "object",
          "properties" => %{"command" => %{"type" => "string", "minLength" => 1}},
          "required" => ["command"],
          "additionalProperties" => false
        },
        fn arguments, cancellation ->
          Bash.run(arguments["command"], cancellation: cancellation)
        end
      )

    Agent.new(provider, model, instructions: @instructions, tools: [tool])
  end

  def turn(history, prompt, provider, model, options \\ []) do
    options =
      Keyword.validate!(options,
        cancellation: CancellationToken.new(),
        on_text: nil,
        on_diagnostic: nil
      )

    result =
      Agent.run(agent(provider, model), prompt,
        history: history,
        cancellation: options[:cancellation],
        on_event: fn event, token ->
          case event.kind do
            {:text_delta, text} ->
              if callback = options[:on_text], do: callback.(text, token)

            {:tool_start, _, _, %{"command" => command}}
            when is_binary(command) and command != "" ->
              if callback = options[:on_diagnostic], do: callback.("\n$ #{command}\n", token)

            {:tool_end, _, _, output, _} ->
              if not CancellationToken.cancelled?(token) and options[:on_diagnostic],
                do: options[:on_diagnostic].(output <> "\n", token)

            _ ->
              :ok
          end
        end
      )

    result.history
  end

  def run(provider, model, options \\ []) do
    options = Keyword.validate!(options, cancellation: CancellationToken.new())
    parent = options[:cancellation]
    CancellationToken.check!(parent)
    token = CancellationToken.child(parent)

    signal =
      case RpcIO.watch_interrupt() do
        {:ok, handle} -> handle
        {:error, message} -> raise ProviderError, message: message
      end

    try do
      with_io(0, 0, fn input ->
        console(1, "TinyAgent. Type a task, or Ctrl-C or Ctrl-D to exit.\n", token, signal)
        prompt(input, "", false, [], provider, model, token, signal)
      end)
    rescue
      error in Cancelled ->
        CancellationToken.check!(parent)
        unless RpcIO.interrupted?(signal), do: reraise(error, __STACKTRACE__)
        :ok
    after
      CancellationToken.cancel(token)
      RpcIO.close(signal)
    end
  end

  defp prompt(input, pending, ended, history, provider, model, token, signal) do
    check(token, signal)
    console(1, "\n> ", token, signal)

    case line(input, pending, ended, token, signal) do
      :eof ->
        console(1, "\n", token, signal)

      {line, rest, ended} ->
        next =
          case String.trim(line) do
            "" -> history
            text -> run_turn(history, text, provider, model, token, signal)
          end

        prompt(input, rest, ended, next, provider, model, token, signal)
    end
  end

  defp line(input, pending, ended, token, signal) do
    check(token, signal)

    case :binary.match(pending, "\n") do
      {index, 1} ->
        {binary_part(pending, 0, index),
         binary_part(pending, index + 1, byte_size(pending) - index - 1), ended}

      :nomatch when ended and pending == "" ->
        :eof

      :nomatch when ended ->
        {pending, "", true}

      :nomatch ->
        case RpcIO.read(input) do
          :again ->
            Process.sleep(10)
            line(input, pending, false, token, signal)

          :eof ->
            line(input, pending, true, token, signal)

          {:ok, bytes} ->
            line(input, pending <> bytes, false, token, signal)

          {:error, message} ->
            raise ProviderError, message: "CLI input failed: #{message}"
        end
    end
  end

  defp run_turn(history, prompt, provider, model, token, signal) do
    worker =
      Task.async(fn ->
        try do
          next =
            with_io(1, 1, fn output ->
              with_io(2, 1, fn errors ->
                turn(history, prompt, provider, model,
                  cancellation: token,
                  on_text: fn text, cancellation -> write(output, text, 0, cancellation, nil) end,
                  on_diagnostic: fn text, cancellation ->
                    write(errors, text, 0, cancellation, nil)
                  end
                )
              end)
            end)

          {:ok, next}
        catch
          kind, reason -> {:raised, kind, reason, __STACKTRACE__}
        end
      end)

    result =
      try do
        await_turn(worker, token, signal)
      catch
        kind, reason ->
          CancellationToken.cancel(token)
          Task.await(worker, :infinity)
          :erlang.raise(kind, reason, __STACKTRACE__)
      end

    check(token, signal)

    case result do
      {:ok, next} ->
        console(1, "\n", token, signal)
        next

      {:raised, :error, %Cancelled{} = error, trace} ->
        :erlang.raise(:error, error, trace)

      {:raised, kind, error, trace} ->
        message = Exception.format_banner(kind, error, trace) |> String.slice(0, 1000)
        console(2, "error: #{message}\n", token, signal)
        history
    end
  end

  defp await_turn(worker, token, signal) do
    if RpcIO.interrupted?(signal), do: CancellationToken.cancel(token)

    case Task.yield(worker, 10) do
      nil -> await_turn(worker, token, signal)
      {:ok, result} -> result
    end
  end

  defp with_io(descriptor, writing, callback) do
    case RpcIO.open(descriptor, writing) do
      {:ok, handle} ->
        try do
          callback.(handle)
        after
          RpcIO.close(handle)
        end

      {:error, message} ->
        raise ProviderError, message: "CLI I/O failed: #{message}"
    end
  end

  defp console(descriptor, text, token, signal),
    do: with_io(descriptor, 1, &write(&1, text, 0, token, signal))

  defp write(handle, text, offset, token, signal) do
    check(token, signal)

    if offset < byte_size(text) do
      case RpcIO.write(handle, text, offset) do
        :again ->
          Process.sleep(10)
          write(handle, text, offset, token, signal)

        {:ok, count} ->
          write(handle, text, offset + count, token, signal)

        {:error, message} ->
          raise ProviderError, message: "CLI output failed: #{message}"
      end
    end
  end

  defp check(token, signal) do
    if signal != nil and RpcIO.interrupted?(signal), do: CancellationToken.cancel(token)
    CancellationToken.check!(token)
  end

  def self_test do
    if Bash.denied_reason("printf 'hello\\n'"), do: raise("Safe command was denied")

    for command <- [
          "rm -rf /",
          ~s(r""m -rf /),
          "sudo echo hi",
          "git reset --hard HEAD",
          "find . -delete",
          "mkfs.ext4 /dev/sda"
        ] do
      unless Bash.denied_reason(command), do: raise("Unsafe command was allowed: #{command}")
    end

    IO.puts("self-test passed")
  end

  def serve(agent) do
    {:ok, signal} = RpcIO.watch_interrupt()

    try do
      {:ok, server} =
        HttpServer.start_link(agent,
          host: System.get_env("TINYAGENT_HOST", "127.0.0.1"),
          port: String.to_integer(System.get_env("TINYAGENT_PORT", "8080")),
          token: System.get_env("TINYAGENT_TOKEN"),
          allow_remote: System.get_env("TINYAGENT_ALLOW_REMOTE") == "1"
        )

      monitor = Process.monitor(server)

      try do
        IO.puts(:stderr, "TinyAgent listening on " <> HttpServer.url(server))
        await_server(server, monitor, signal)
      after
        HttpServer.close(server)
        Process.demonitor(monitor, [:flush])
      end
    after
      RpcIO.close(signal)
    end
  end

  defp await_server(server, monitor, signal) do
    if RpcIO.interrupted?(signal) do
      HttpServer.close(server)
    else
      receive do
        {:DOWN, ^monitor, :process, ^server, :normal} -> :ok
        {:DOWN, ^monitor, :process, ^server, reason} -> exit(reason)
      after
        10 -> await_server(server, monitor, signal)
      end
    end
  end

  def main(arguments) do
    provider = OpenAIProvider.new()
    model = System.get_env("TINYAGENT_MODEL", "gpt-5.6")

    case arguments do
      ["--self-test"] -> self_test()
      ["--mode", "rpc"] -> Rpc.run_stdio(agent(provider, model))
      ["serve"] -> serve(agent(provider, model))
      [] -> run(provider, model)
      _ -> raise ArgumentError, "Usage: tiny_agent.exs [--self-test | --mode rpc | serve]"
    end
  end
end
