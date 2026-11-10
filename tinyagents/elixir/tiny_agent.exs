#!/usr/bin/env -S elixir --erl +B

unless Code.ensure_loaded?(TinyAgent.Bash) do
  Mix.install([{:tiny_agent_sdk, path: __DIR__}])
end

Code.require_file("cli.ex", __DIR__)
TinyAgent.CLI.main(System.argv())
