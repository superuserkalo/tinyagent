defmodule TinyAgent.RpcIOTest do
  use ExUnit.Case, async: true

  for scenario <- ~w(echo files stalled broken owner interrupt signal_owner invalid) do
    @scenario scenario
    test "native I/O #{@scenario}" do
      beam = :code.which(TinyAgent.RpcIO) |> to_string() |> Path.dirname()
      script = Path.join(__DIR__, "rpc_io_native.py")
      {output, status} = System.cmd("python3", [script, @scenario, beam], stderr_to_stdout: true)
      assert status == 0, output
      assert output == @scenario <> " passed\n"
    end
  end
end
