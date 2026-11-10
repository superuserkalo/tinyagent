"""Pipe client shared by native Elixir RPC subprocess checks."""

import json
import os
from pathlib import Path
import selectors
import subprocess
import time


LIMIT = 1_048_576


class Client:
    def __init__(
        self, beam, jason, control=False, fixture="rpc_fixture.exs", arguments=()
    ):
        self.control = None
        inherited = []
        if control:
            read_fd, self.control = os.pipe()
            inherited = [read_fd]
        self.process = subprocess.Popen(
            [
                "elixir",
                "--erl",
                "+B i",
                "-pa",
                beam,
                "-pa",
                jason,
                str(Path(__file__).with_name(fixture)),
                *arguments,
                *map(str, inherited),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            pass_fds=tuple(inherited),
        )
        for fd in inherited:
            os.close(fd)
        self.buffers = {"stdout": b"", "stderr": b""}
        self.stderr = []

    def send_bytes(self, data):
        view = memoryview(data)
        while view:
            count = self.process.stdin.write(view)
            assert count
            view = view[count:]

    def send(self, command):
        self.send_bytes(
            json.dumps(command, ensure_ascii=False, separators=(",", ":")).encode()
            + b"\n"
        )

    def line(self, name="stdout"):
        pipe = getattr(self.process, name)
        deadline = time.monotonic() + 5
        while b"\n" not in self.buffers[name]:
            with selectors.DefaultSelector() as selector:
                selector.register(pipe, selectors.EVENT_READ)
                assert selector.select(max(0, deadline - time.monotonic())), (
                    "Timed out",
                    name,
                    self.buffers,
                )
            data = os.read(pipe.fileno(), 8192)
            assert data, ("Unexpected EOF", name, self.buffers)
            self.buffers[name] += data
        line, self.buffers[name] = self.buffers[name].split(b"\n", 1)
        if name == "stderr":
            self.stderr.append(line.decode())
        return line

    def value(self):
        line = self.line()
        assert len(line) <= LIMIT
        return json.loads(line)

    def prompt(self, message, identifier="prompt"):
        self.send({"id": identifier, "type": "prompt", "message": message})

    def terminal(self):
        values = []
        while not values or values[-1].get("type") != "agent_end":
            values.append(self.value())
        return values

    def state(self):
        self.send({"id": "state", "type": "get_state"})
        while True:
            value = self.value()
            if value.get("id") == "state":
                return value["data"]

    def idle(self):
        deadline = time.monotonic() + 3
        while True:
            state = self.state()
            if state["status"] == "idle":
                return state
            assert time.monotonic() < deadline

    def finish(self, status=0, complete_lines=True):
        self.process.stdin.close()
        deadline = time.monotonic() + 5
        with selectors.DefaultSelector() as selector:
            if self.process.stdout.closed:
                return self.collect(status, complete_lines)
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while True:
                assert selector.select(
                    max(0, deadline - time.monotonic())
                ), "RPC did not close stdout"
                data = os.read(self.process.stdout.fileno(), 8192)
                if not data:
                    break
                self.buffers["stdout"] += data
        return self.collect(status, complete_lines)

    def collect(self, status, complete_lines):
        self.process.wait(timeout=3)
        errors = self.buffers["stderr"] + self.process.stderr.read()
        self.stderr += errors.decode().splitlines()
        assert self.process.returncode == status, (self.process.returncode, self.stderr)
        data = self.buffers["stdout"]
        self.buffers["stdout"] = b""
        if not complete_lines:
            return data
        assert not data or data.endswith(b"\n"), data[-200:]
        assert all(len(line) <= LIMIT for line in data.splitlines())
        return [json.loads(line) for line in data.splitlines()]

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=3)
        for pipe in [self.process.stdin, self.process.stdout, self.process.stderr]:
            pipe.close()
        if self.control is not None:
            os.close(self.control)
