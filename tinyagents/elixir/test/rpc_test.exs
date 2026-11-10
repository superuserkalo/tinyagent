defmodule TinyAgent.RpcTest do
  use ExUnit.Case, async: true

  for {script, scenarios} <- [
        {"rpc_native.py",
         ~w(shared completion abort eof interrupt errors framing utf8 oversized_input exact_limit reset_cleanup)},
        {"rpc_output_native.py",
         ~w(output_limit oversized_ack oversized_reply stalled_eof broken_pipe queue_limit abort_terminal abort_pending_ack eof_cleanup)},
        {"rpc_api_native.py", ~w(invalid external_cancel owner_exit)}
      ] do
    @script Path.join(__DIR__, script)

    for scenario <- scenarios do
      @scenario scenario
      test "native RPC #{@scenario}" do
        beam = :code.which(TinyAgent.Rpc) |> to_string() |> Path.dirname()
        jason = :code.which(Jason) |> to_string() |> Path.dirname()

        {output, status} =
          System.cmd("python3", [@script, @scenario, beam, jason], stderr_to_stdout: true)

        assert status == 0, output
        assert output == @scenario <> " passed\n"
      end
    end
  end
end
