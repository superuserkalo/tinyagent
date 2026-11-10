#!/usr/bin/env python3
"""Exercise the compiled coding CLI and its console over native pipes."""

from contextlib import contextmanager
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlsplit


CLI = str(Path(sys.argv.pop(1)).resolve())
FIXTURE = str(Path(sys.argv.pop(1)).resolve())
ENV = {key: value for key, value in os.environ.items() if not key.startswith("TINYAGENT_") and key not in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"}}


@contextmanager
def process(executable, args=(), environment=None):
    child = subprocess.Popen(
        [executable, *args], env=ENV | (environment or {}),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
    )
    try:
        yield child
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def until(pipe, expected):
    data = bytearray()
    deadline = time.monotonic() + 5
    while expected not in data:
        ready = select.select([pipe], [], [], max(0, deadline - time.monotonic()))[0]
        if not ready:
            raise AssertionError(f"No native output matching {expected!r}")
        chunk = os.read(pipe.fileno(), 4096)
        if not chunk:
            raise AssertionError(f"Process ended before {expected!r}: {bytes(data)!r}")
        data.extend(chunk)
    return bytes(data)


class CliTests(unittest.TestCase):
    def finish(self, child, status=0):
        if child.stdin is not None:
            child.stdin.close()
            child.stdin = None
        output, errors = child.communicate(timeout=3)
        self.assertEqual(status, child.returncode, errors)
        self.assertNotIn(b"error(gpa)", errors)
        return output, errors

    def test_options_and_self_test_without_credentials(self):
        result = subprocess.run([CLI, "--self-test"], env=ENV, capture_output=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(b"self-test passed\n", result.stdout)
        self.assertEqual(b"", result.stderr)
        for args, error in (
            ([], b"MissingOpenAiApiKey"), (["--mode"], b"usage:"),
            (["--mode", "bad"], b"usage:"), (["serve", "--port", "65536"], b"InvalidPort"),
            (["serve", "--host", "example.com"], b"InvalidHost"),
            (["serve", "--unknown"], b"usage:"),
        ):
            result = subprocess.run([CLI, *args], env=ENV, capture_output=True, timeout=5)
            self.assertEqual(1, result.returncode)
            self.assertIn(error, result.stderr)
            self.assertEqual(b"", result.stdout)

    def test_cli_idle_eof_and_interrupt_with_either_provider(self):
        for provider, key in (("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")):
            for interrupt in (False, True):
                with self.subTest(provider=provider, interrupt=interrupt), process(CLI, environment={"TINYAGENT_PROVIDER": provider, key: "test-key"}) as child:
                    until(child.stdout, b"\n> ")
                    if interrupt:
                        child.send_signal(signal.SIGINT)
                        child.wait(timeout=3)
                    _, errors = self.finish(child)
                    self.assertEqual(b"", errors)

    def test_cli_rpc_uses_only_protocol_stdout_and_joins_interrupt(self):
        with process(CLI, ["--mode", "rpc"], {"OPENAI_API_KEY": "test-key"}) as child:
            child.stdin.write(b'{"id":"state","type":"get_state"}\n')
            reply = json.loads(until(child.stdout, b"\n"))
            self.assertEqual("response", reply["type"])
            self.assertEqual({"protocolVersion": 1, "status": "idle", "runId": None, "messageCount": 0}, reply["data"])
            child.send_signal(signal.SIGINT)
            child.wait(timeout=3)
            self.assertEqual((b"", b""), self.finish(child))

    def test_cli_http_dispatch_authentication_and_shutdown(self):
        with process(CLI, ["serve", "--port", "0"], {"OPENAI_API_KEY": "test-key", "TINYAGENT_TOKEN": "secret"}) as child:
            startup = until(child.stderr, b"\n").decode().strip()
            self.assertTrue(startup.startswith("TinyAgent listening on "))
            url = urlsplit(startup.removeprefix("TinyAgent listening on "))
            self.assertEqual("127.0.0.1", url.hostname)
            for method, path, token, expected in (("GET", "/health", "wrong", 401), ("GET", "/health", "secret", 200), ("POST", "/sessions", "secret", 201)):
                connection = HTTPConnection(url.hostname, url.port, timeout=3)
                try:
                    connection.request(method, path, headers={"Authorization": "Bearer " + token})
                    response = connection.getresponse()
                    self.assertEqual(expected, response.status)
                    self.assertFalse(any(key.lower().startswith("access-control-") for key, _ in response.getheaders()))
                    response.read()
                finally:
                    connection.close()
            child.send_signal(signal.SIGTERM)
            child.wait(timeout=3)
            self.assertEqual((b"", b""), self.finish(child))

    def test_console_utf8_final_line_and_exact_input_limit(self):
        for source, status, expected in (
            ("café\nlast".encode(), 0, "café\n\n> last\n".encode()),
            (b"x" * 1_048_576 + b"\n", 0, b"accepted"),
            (b"x" * 1_048_577, 1, b"InputTooLarge"),
            (b"\xff\n", 1, b"InvalidUtf8"),
        ):
            with self.subTest(size=len(source), status=status):
                result = subprocess.run([FIXTURE], input=source, env=ENV, capture_output=True, timeout=5)
                self.assertEqual(status, result.returncode, result.stderr)
                self.assertIn(expected, result.stdout if status == 0 else result.stderr)
                self.assertNotIn(b"error(gpa)", result.stderr)

    def test_console_cancellation_joins_a_waiting_provider(self):
        with process(FIXTURE) as child:
            until(child.stdout, b"\n> ")
            child.stdin.write(b"wait\n")
            until(child.stdout, b"waiting")
            child.send_signal(signal.SIGINT)
            child.wait(timeout=3)
            self.assertEqual((b"", b""), self.finish(child))

    def test_console_cancellation_bounds_stalled_output(self):
        for message, diagnostics, expected in (
            ("large", False, b"x"), ("final_large", False, b"x"),
            ("tool_start", True, b"$ printf"), ("tool_end", True, b"exit_code:"),
        ):
            with self.subTest(message=message), process(FIXTURE) as child:
                until(child.stdout, b"\n> ")
                child.stdin.write(message.encode() + b"\n")
                until(child.stderr if diagnostics else child.stdout, expected)
                child.send_signal(signal.SIGINT)
                # Do not drain the blocked destination until the process exits.
                child.wait(timeout=3)
                self.finish(child, status=1)

    def test_console_broken_stdout_releases_provider(self):
        with process(FIXTURE) as child:
            until(child.stdout, b"\n> ")
            child.stdout.close()
            child.stdout = None
            child.stdin.write(b"large\n")
            child.wait(timeout=3)
            _, errors = self.finish(child, status=1)
            self.assertNotIn(b"ProviderLeaked", errors)

    def test_console_cancellation_stops_native_bash_descendants(self):
        Path(".zig-cache").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="console-", dir=".zig-cache") as directory:
            ready = Path(directory) / "ready"
            command = "sleep 30 & printf '%s %s\\n' $$ $! > " + shlex.quote(str(ready)) + "; wait"
            with process(FIXTURE, environment={"TINYAGENT_TEST_BASH": command}) as child:
                until(child.stdout, b"\n> ")
                child.stdin.write(b"bash\n")
                deadline = time.monotonic() + 5
                while not ready.exists() or not ready.read_bytes().endswith(b"\n"):
                    self.assertLess(time.monotonic(), deadline, "Bash did not enter")
                    time.sleep(0.005)
                pids = [int(value) for value in ready.read_text().split()]
                cleaned = False
                try:
                    child.send_signal(signal.SIGINT)
                    child.wait(timeout=3)
                    self.finish(child)
                    deadline = time.monotonic() + 2
                    for pid in pids:
                        while True:
                            try:
                                os.kill(pid, 0)
                            except ProcessLookupError:
                                break
                            self.assertLess(time.monotonic(), deadline, f"Bash process {pid} survived")
                            time.sleep(0.005)
                    cleaned = True
                finally:
                    if not cleaned:
                        try:
                            os.killpg(pids[0], signal.SIGKILL)
                        except ProcessLookupError:
                            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
