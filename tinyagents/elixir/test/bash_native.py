"""Verify Elixir Bash process ownership over real pipes, without provider calls."""

import errno
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time


def line(process):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(5), "Bash fixture did not respond"
    return process.stdout.readline().decode().rstrip()


def gone(pids):
    for pid in pids:
        try:
            os.kill(pid, 0)
            return False
        except OSError as error:
            assert error.errno == errno.ESRCH, error
    return True


def main():
    mode, beam = sys.argv[1:]
    with tempfile.TemporaryDirectory(
        prefix="bash-", dir=Path(beam).parent
    ) as directory:
        root = Path(directory)
        pid_file = root / "pids"
        environment = os.environ.copy()
        if mode == "environment":
            startup = root / "startup"
            startup.write_text(f"printf loaded > '{root / 'loaded'}'\n")
            environment["BASH_ENV"] = str(startup)
            environment["ENV"] = str(startup)
        process = subprocess.Popen(
            [
                "elixir",
                "-pa",
                beam,
                str(Path(__file__).with_name("bash_fixture.exs")),
                mode,
                str(pid_file),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            bufsize=0,
        )
        try:
            if mode in {"cancel", "owner", "independent"}:
                assert line(process) == "ready"
                pids = [int(value) for value in pid_file.read_text().split()]
                assert len(pids) == 2 and all(pid > 0 for pid in pids)
                for pid in pids:
                    os.kill(pid, 0)
                process.stdin.write(b"c")
                assert line(process) == "closed"
                deadline = time.monotonic() + 3
                while not gone(pids):
                    assert time.monotonic() < deadline, "Bash child survived cleanup"
                    time.sleep(0.005)
                process.stdin.write(b"x")
            output, errors = process.communicate(timeout=5)
            assert process.returncode == 0 and not output and not errors, (
                process.returncode,
                output,
                errors,
            )
            if mode in {"background", "overflow"}:
                pids = [int(value) for value in pid_file.read_text().split()]
                deadline = time.monotonic() + 3
                while not gone(pids):
                    assert (
                        time.monotonic() < deadline
                    ), "Background child survived cleanup"
                    time.sleep(0.005)
            assert not (root / "loaded").exists(), "Bash sourced a startup file"
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    print(mode + " passed")


if __name__ == "__main__":
    main()
