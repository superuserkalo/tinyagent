Code.require_file("native.exs", __DIR__)

defmodule Mix.Tasks.Compile.TinyAgentNative do
  use Mix.Task.Compiler

  def run(_arguments) do
    TinyAgent.NativeBuild.compile(__DIR__, Mix.Project.app_path())
    {:ok, []}
  end
end

defmodule TinyAgent.MixProject do
  use Mix.Project

  def project do
    [
      app: :tiny_agent_sdk,
      version: "0.0.1",
      elixir: "~> 1.15",
      compilers: [:tiny_agent_native] ++ Mix.compilers(),
      description: "Native agent core and streaming providers for TinyAgent.",
      deps: [
        {:mint, "1.10.0"},
        {:jason, "1.4.5"},
        {:bandit, "1.12.5"},
        {:plug, "1.20.3"},
        {:thousand_island, "1.5.0"},
        {:jsv, "0.22.0"},
        {:abnf_parsec, "2.1.0"},
        {:texture, "1.2.1"},
        {:idna, "7.1.0"},
        {:nimble_parsec, "1.4.2"}
      ],
      package: [
        files: ["lib", "c_src", "native.exs", "mix.exs", "mix.lock", "README.md", "LICENSE"],
        licenses: ["MIT"],
        links: %{}
      ]
    ]
  end

  def application, do: [extra_applications: [:logger, :ssl, :crypto]]
end
