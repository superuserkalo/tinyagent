defmodule TinyAgent.BashTest do
  use ExUnit.Case, async: true
  alias TinyAgent.{Bash, CancellationToken, Cancelled, ProviderError}

  test "guards, argument validation, and bounded denial messages" do
    assert Bash.denied_reason("printf 'hello\\n'") == nil

    for command <- [
          "rm -rf /",
          ~s(r""m -rf /),
          "sudo echo hi",
          "git reset --hard HEAD",
          "find . -delete",
          "mkfs.ext4 /dev/sda",
          "git checkout -- file",
          ":(){ :|:& };:"
        ] do
      assert Bash.denied_reason(command)
      assert Bash.run(command) =~ "exit_code: 126\n"
    end

    for command <- [nil, 1, "", " \n", <<255>>, "echo\0oops"] do
      assert_raise ArgumentError, fn -> Bash.run(command) end
    end

    assert byte_size(Bash.run("mkfs." <> String.duplicate("x", 200_000))) < 256
  end

  test "separate output streams, status, closed stdin, and invalid UTF-8" do
    assert Bash.run("printf 'hello\\n'; printf 'problem\\n' >&2; exit 7") ==
             "exit_code: 7\nstdout:\nhello\nstderr:\nproblem\n"

    assert Bash.run("if read value; then echo unexpected; else echo eof; fi") =~ "stdout:\neof\n"
    assert Bash.run("printf '\\377hello'") =~ "�hello"
  end

  test "combined raw output, envelope, and replacement expansion are bounded" do
    for command <- [
          "printf '%070000d' 0; printf '%070000d' 0 >&2",
          "printf '%0131050d' 0",
          "for ((i=0;i<50000;i++)); do printf '\\377'; done",
          "while :; do printf '%08192d' 0; done"
        ] do
      assert_raise ProviderError, "Bash output exceeded 131072 UTF-8 bytes.", fn ->
        Bash.run(command)
      end
    end

    assert byte_size(Bash.run("printf '%0131000d' 0")) <= Bash.max_output_bytes()
  end

  test "pre-cancellation starts no command and retains its reason" do
    token = CancellationToken.new()
    CancellationToken.cancel(token)
    assert_raise Cancelled, fn -> Bash.run("printf unexpected", cancellation: token) end
  end

  test "completed and failed calls leave caller messages and trap_exit unchanged" do
    previous = Process.flag(:trap_exit, true)
    marker = make_ref()
    send(self(), {:foreign, marker})

    try do
      assert Bash.run("printf hello") =~ "hello"
      assert_raise ProviderError, fn -> Bash.run("printf '%0140000d' 0") end
      assert_receive {:foreign, ^marker}
      refute_receive _message, 20
      assert Process.info(self(), :trap_exit) == {:trap_exit, true}
    after
      Process.flag(:trap_exit, previous)
    end
  end

  for mode <- ~w(cancel owner independent background overflow environment) do
    @mode mode
    test "native Bash #{@mode}" do
      beam = :code.which(Bash) |> to_string() |> Path.dirname()
      script = Path.join(__DIR__, "bash_native.py")
      {output, status} = System.cmd("python3", [script, @mode, beam], stderr_to_stdout: true)
      assert status == 0, output
      assert output == @mode <> " passed\n"
    end
  end
end
