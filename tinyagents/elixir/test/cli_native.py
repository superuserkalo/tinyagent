"""Exercise the Elixir coding CLI with native descriptors and offline providers."""

import array
import fcntl
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import termios
import time

from bash_native import gone


def line(process):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stderr, selectors.EVENT_READ)
        assert selector.select(5), "CLI fixture did not respond"
    return process.stderr.readline().decode().rstrip()


def until(process, expected):
    for _ in range(20):
        value = line(process)
        if value == expected:
            return
    raise AssertionError(f"Missing CLI fixture note: {expected}")


def main():
    mode, beam, cli, *dependencies = sys.argv[1:]
    with tempfile.TemporaryDirectory(prefix="cli-", dir=Path(beam).parent) as directory:
        root = Path(directory)
        pid_file = root / "pids"
        command = ["elixir", "--erl", "+B i", "-pa", beam]
        for dependency in dependencies:
            command += ["-pa", dependency]
        command += [
            "-r",
            cli,
            str(Path(__file__).with_name("cli_fixture.exs")),
            mode,
            str(pid_file),
        ]
        if mode in {"conversation", "rollback"}:
            source = root / "input"
            source.write_bytes(
                b"first\nfail\nsecond" if mode == "rollback" else b"first\nsecond"
            )
            target = root / "output"
            errors = root / "errors"
            with source.open("rb") as reader, target.open("wb") as writer, errors.open(
                "wb"
            ) as stderr:
                streams = [reader, writer, stderr]
                before = [
                    fcntl.fcntl(stream, fcntl.F_GETFL) & os.O_NONBLOCK
                    for stream in streams
                ]
                result = subprocess.run(
                    command, stdin=reader, stdout=writer, stderr=stderr, timeout=10
                )
                assert result.returncode == 0, errors.read_text()
                assert before == [
                    fcntl.fcntl(stream, fcntl.F_GETFL) & os.O_NONBLOCK
                    for stream in streams
                ]
            notes = errors.read_text().splitlines()
            assert [note for note in notes if note.startswith("request:")] == (
                ["request:1", "request:3", "request:3"]
                if mode == "rollback"
                else ["request:1", "request:3"]
            )
            assert notes[-1] == "restored", notes
            assert "done" in target.read_text()
        else:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    assert selector.select(5)
                assert process.stdout.read(1), process.communicate(timeout=5)[1]
                pids = []
                if mode != "idle":
                    process.stdin.write(b"go\n")
                    until(process, "request:1")
                    if mode == "bash":
                        deadline = time.monotonic() + 3
                        while not pid_file.exists() or not pid_file.read_text():
                            assert time.monotonic() < deadline, "CLI Bash did not start"
                            time.sleep(0.005)
                        pids = [int(value) for value in pid_file.read_text().split()]
                    if mode in {"stalled", "broken"}:
                        # Filling stdout proves the callback entered its native write.
                        deadline = time.monotonic() + 3
                        while True:
                            available = array.array("i", [0])
                            fcntl.ioctl(process.stdout, termios.FIONREAD, available)
                            if available[0] >= 8192:
                                break
                            assert (
                                time.monotonic() < deadline
                            ), "CLI output did not fill its pipe"
                            time.sleep(0.005)
                if mode == "broken":
                    process.stdout.close()
                else:
                    process.send_signal(signal.SIGINT)
                until(process, "restored")
                if pids:
                    assert gone(pids), "CLI returned before its Bash group stopped"
                process.send_signal(signal.SIGINT)
                time.sleep(0.02)
                assert (
                    process.poll() is None
                ), "CLI did not restore the previous SIGINT action"
                process.stdin.write(b"q")
                process.wait(timeout=5)
                assert process.returncode == (1 if mode == "broken" else 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in [process.stdin, process.stdout, process.stderr]:
                    if not stream.closed:
                        stream.close()
    print(mode + " passed")


if __name__ == "__main__":
    main()
