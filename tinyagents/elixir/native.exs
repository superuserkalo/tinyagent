defmodule TinyAgent.NativeBuild do
  @moduledoc false

  def compile(root, application) do
    case :os.type() do
      {:unix, :darwin} ->
        compile(root, application, ["-dynamiclib", "-undefined", "dynamic_lookup"])

      {:unix, :linux} ->
        compile(root, application, ["-shared", "-fPIC"])

      _platform ->
        :ok
    end
  end

  defp compile(root, application, linker) do
    include = Path.join(to_string(:code.root_dir()), "usr/include")

    compile_file(
      root,
      application,
      "rpc_io.c",
      "tiny_agent_io.so",
      linker ++ ["-pthread", "-I", include]
    )

    compile_file(root, application, "bash.c", "tiny_agent_bash", [])
  end

  defp compile_file(root, application, source, target, flags) do
    output = Path.join(application, "priv/#{target}")
    temporary = output <> ".#{System.pid()}.#{:erlang.unique_integer([:positive])}"
    File.mkdir_p!(Path.dirname(output))
    compiler = System.get_env("CC", "cc")

    arguments =
      ["-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-Wpedantic"] ++
        flags ++ [Path.join(root, "c_src/#{source}"), "-o", temporary]

    try do
      case System.cmd(compiler, arguments, stderr_to_stdout: true) do
        {"", 0} ->
          File.rename!(temporary, output)

        {diagnostics, 0} ->
          raise "Native compiler emitted diagnostics for #{source}: #{diagnostics}"

        {diagnostics, status} ->
          raise "Native compilation failed for #{source} (#{status}): #{diagnostics}"
      end
    after
      File.rm(temporary)
    end
  end
end
