#!/usr/bin/env elixir

defmodule TinyAgent.Check do
  def run do
    {options, rest} =
      OptionParser.parse!(System.argv(), strict: [offline: :boolean, sdk_path: :string])

    if rest != [], do: raise(ArgumentError, "Unexpected arguments: #{inspect(rest)}")
    root = Path.expand(Keyword.get(options, :sdk_path, __DIR__))
    Mix.start()
    Code.require_file(Path.join(root, "mix.exs"))
    project = Mix.Project.config()

    unless Version.match?(System.version(), project[:elixir]),
      do: raise("Unsupported Elixir version.")

    lock = Mix.Dep.Lock.read(Path.join(root, "mix.lock"))
    dependencies = System.get_env("TINYAGENT_ELIXIR_DEPS", Path.join(__DIR__, "_build/dev/lib"))

    for app <- Map.keys(lock) do
      path = Path.join([dependencies, Atom.to_string(app), "ebin"])

      unless File.dir?(path),
        do:
          raise(
            "Missing #{app} build. Run mix deps.get and mix compile, or set TINYAGENT_ELIXIR_DEPS to an existing compiled dependency directory."
          )

      Code.prepend_path(path)
    end

    for app <- Map.keys(lock) do
      {:ok, _} = Application.ensure_all_started(app)
      version = Application.spec(app, :vsn) |> to_string()

      unless version == elem(Map.fetch!(lock, app), 2),
        do: raise("#{app} build does not match mix.lock.")

      if declared = project[:deps][app], do: true = Version.match?(version, declared)
      IO.puts("#{app} #{version}")
    end

    files = Path.wildcard(Path.join(root, "lib/**/*.ex"))
    cli = System.get_env("TINYAGENT_ELIXIR_CLI", Path.join(__DIR__, "cli.ex"))
    check_format(files ++ Enum.map(["mix.exs", "native.exs"], &Path.join(root, &1)))

    check_format(
      Path.wildcard(Path.join(__DIR__, "{test,examples}/**/*.{ex,exs}")) ++
        [
          __ENV__.file,
          Path.join(__DIR__, ".formatter.exs"),
          cli,
          Path.join(__DIR__, "tiny_agent.exs")
        ]
    )

    output = Path.join(root, "_build/direct/lib/tiny_agent_sdk/ebin")
    File.mkdir_p!(output)
    TinyAgent.NativeBuild.compile(root, Path.dirname(output))

    case Kernel.ParallelCompiler.compile_to_path(files, output,
           warnings_as_errors: true,
           return_diagnostics: true
         ) do
      {:ok, _, []} -> :ok
      {:ok, _, %{compile_warnings: [], runtime_warnings: []}} -> :ok
      result -> raise "Compilation failed: #{inspect(result)}"
    end

    Code.prepend_path(output)
    IO.puts("Compiled #{length(files)} SDK source files from #{root}")
    cli_output = Path.join(root, "_build/direct/cli/ebin")
    File.mkdir_p!(cli_output)

    case Kernel.ParallelCompiler.compile_to_path([cli], cli_output,
           warnings_as_errors: true,
           return_diagnostics: true
         ) do
      {:ok, _, []} -> :ok
      {:ok, _, %{compile_warnings: [], runtime_warnings: []}} -> :ok
      result -> raise "CLI compilation failed: #{inspect(result)}"
    end

    Code.prepend_path(cli_output)
    Code.require_file(Path.join(__DIR__, "test/test_helper.exs"))
    ExUnit.configure(autorun: false, exclude: if(options[:offline], do: [:tcp], else: []))
    for file <- Path.wildcard(Path.join(__DIR__, "test/*_test.exs")), do: Code.require_file(file)
    result = ExUnit.run()
    TinyAgent.CLI.self_test()
    System.halt(if result.failures == 0, do: 0, else: 1)
  end

  defp check_format(files) do
    for file <- files do
      source = File.read!(file)
      formatted = IO.iodata_to_binary([Code.format_string!(source), "\n"])
      unless source == formatted, do: raise("Run mix format: #{file}")
    end
  end
end

TinyAgent.Check.run()
