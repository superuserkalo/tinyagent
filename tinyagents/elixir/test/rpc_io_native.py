"""Drive the Elixir NIF over real process pipes; this is test orchestration only."""

import fcntl
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time


def line(process):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stderr, selectors.EVENT_READ)
        assert selector.select(5), "Elixir fixture did not respond"
    return process.stderr.readline().decode().rstrip()


def send(process):
    process.stdin.write(b"x")
    process.stdin.flush()


def main():
    mode, beam = sys.argv[1:]
    command = [
        "elixir",
        "--erl",
        "+B i",
        "-pa",
        beam,
        str(Path(__file__).with_name("rpc_io_fixture.exs")),
        mode,
    ]
    if mode == "files":
        with tempfile.TemporaryDirectory(
            prefix="rpc-files-", dir=Path(beam).parent
        ) as directory:
            source = Path(directory) / "input"
            target = Path(directory) / "output"
            payload = bytes(range(256)) * 2048
            source.write_bytes(payload)
            with source.open("rb") as reader, target.open("wb") as writer:
                flags = [
                    fcntl.fcntl(stream, fcntl.F_GETFL) for stream in [reader, writer]
                ]
                result = subprocess.run(
                    command + [str(reader.fileno()), str(writer.fileno())],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    pass_fds=(reader.fileno(), writer.fileno()),
                    timeout=10,
                )
                assert result.returncode == 0 and not result.stderr, result.stderr
                assert not result.stdout
                restored = [
                    fcntl.fcntl(stream, fcntl.F_GETFL) for stream in [reader, writer]
                ]
                # Kernels may also record that a file was written; compare public status flags.
                mask = (
                    os.O_ACCMODE
                    | os.O_NONBLOCK
                    | os.O_APPEND
                    | os.O_ASYNC
                    | os.O_SYNC
                    | os.O_DSYNC
                )
                assert [value & mask for value in flags] == [
                    value & mask for value in restored
                ]
            assert target.read_bytes() == payload
        print("files passed")
        return
    descriptors = []
    arguments = []
    if mode == "owner":
        read_fd, write_fd = os.pipe()
        output_read, output_write = os.pipe()
        descriptors = [read_fd, write_fd, output_read, output_write]
        arguments = [str(read_fd), str(output_write)]
    process = subprocess.Popen(
        command + arguments,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=tuple(descriptors),
        bufsize=0,
    )
    try:
        if mode == "echo":
            payload = bytes(range(256)) * 8192 + "café\n".encode()
            output, errors = process.communicate(payload, timeout=10)
            assert output == payload and not errors, (len(output), errors)
        elif mode == "stalled":
            status = line(process)
            assert status.startswith("stalled:"), status
            assert 0 < int(status.split(":")[1]) < 1_048_576
            assert line(process) == "closed"
            process.wait(timeout=3)
            output, errors = process.communicate(timeout=3)
            assert 0 < len(output) < 1_048_576 and not errors, errors
        elif mode == "broken":
            assert line(process) == "ready"
            process.stdout.close()
            send(process)
            assert line(process) == "closed"
            process.wait(timeout=3)
            assert process.stderr.read() == b""
        elif mode == "owner":
            assert line(process) == "ready"
            for fd in [read_fd, output_write]:
                assert fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_NONBLOCK
            send(process)
            assert line(process) == "restored"
            deadline = time.monotonic() + 1
            while any(
                fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_NONBLOCK
                for fd in [read_fd, output_write]
            ):
                assert (
                    time.monotonic() < deadline
                ), "Owner exit did not restore descriptors"
                time.sleep(0.001)
            send(process)
            output, errors = process.communicate(timeout=3)
            assert not output and not errors, errors
        elif mode in {"interrupt", "signal_owner"}:
            if mode == "interrupt":
                assert line(process) == "ready"
                process.send_signal(signal.SIGINT)
            assert line(process) == "restored"
            process.send_signal(signal.SIGINT)
            time.sleep(0.05)
            assert (
                process.poll() is None
            ), "Previous SIGINT disposition was not restored"
            send(process)
            output, errors = process.communicate(timeout=3)
            assert not output and not errors, errors
        elif mode == "invalid":
            assert line(process) == "checked"
            output, errors = process.communicate(timeout=3)
            assert not output and not errors, errors
        assert process.returncode == 0, process.returncode
        print(mode + " passed")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        for pipe in [process.stdin, process.stdout, process.stderr]:
            if pipe and not pipe.closed:
                pipe.close()
        for fd in descriptors:
            os.close(fd)


if __name__ == "__main__":
    main()
