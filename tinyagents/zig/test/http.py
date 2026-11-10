#!/usr/bin/env python3
"""Exercise the native Zig HTTP server over real TCP, with no model calls."""

import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import time
import unittest
from urllib.parse import urlsplit


EXECUTABLE = str(Path(sys.argv.pop(1)).resolve())
LIMIT = 1_048_576


class Wire:
    def __init__(self, sock):
        self.socket = sock
        self.buffer = bytearray()

    def line(self):
        while b"\n" not in self.buffer:
            data = self.socket.recv(65536)
            if not data:
                if self.buffer:
                    raise AssertionError("Partial response line")
                return b""
            self.buffer.extend(data)
        line, _, rest = self.buffer.partition(b"\n")
        self.buffer = bytearray(rest)
        if len(line) > LIMIT:
            raise AssertionError("HTTP line exceeded its byte limit")
        return line + b"\n"

    def head(self):
        first = self.line().decode().split()
        if len(first) < 2:
            raise AssertionError("Response ended before headers")
        headers = {}
        while (line := self.line()) != b"\r\n":
            if not line:
                raise AssertionError("Incomplete HTTP headers")
            name, value = line.decode().split(":", 1)
            headers[name.lower()] = value.strip()
        return int(first[1]), headers

    def response(self):
        status, headers = self.head()
        if "content-length" in headers:
            length = int(headers["content-length"])
        elif status == 204:
            length = 0
        else:
            length = None
        while length is None or len(self.buffer) < length:
            data = self.socket.recv(65536 if length is None else length - len(self.buffer))
            if not data:
                break
            self.buffer.extend(data)
        body = bytes(self.buffer)
        self.buffer.clear()
        if "content-length" in headers:
            if len(body) != int(headers["content-length"]):
                raise AssertionError("Incomplete HTTP body")
        return status, headers, body

    def events(self):
        result = []
        while line := self.line():
            result.append(json.loads(line))
        return result

    def close(self):
        self.socket.close()


class Server:
    def __init__(self, host="127.0.0.1", token=None, remote=False):
        self.child = subprocess.Popen(
            [EXECUTABLE, host, token if token is not None else "-", str(remote).lower()],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
        )
        self.output = bytearray()
        self.diagnostics = bytearray()
        self.diagnostic_log = bytearray()
        self.wires = []
        self.startup = self.line(self.child.stdout, self.output).decode().strip()
        self.url = urlsplit(self.startup) if not self.startup.startswith("error:") else None
        self.token = token

    def __enter__(self):
        return self

    def __exit__(self, *_):
        for wire in self.wires:
            wire.close()
        if self.child.poll() is None:
            self.child.kill()
        self.child.communicate(timeout=5)

    def line(self, pipe, buffer):
        deadline = time.monotonic() + 10
        while b"\n" not in buffer:
            ready, _, _ = select.select([pipe], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                raise AssertionError("Native fixture did not respond")
            data = os.read(pipe.fileno(), 4096)
            if not data:
                raise AssertionError(f"Native fixture exited: {self.child.poll()}")
            buffer.extend(data)
            if pipe is self.child.stderr:
                self.diagnostic_log.extend(data)
        line, _, remaining = buffer.partition(b"\n")
        buffer[:] = remaining
        return line

    def marker(self, expected):
        while self.line(self.child.stderr, self.diagnostics).decode() != expected:
            pass

    def command(self, value):
        self.child.stdin.write(value.encode() + b"\n")

    def state(self, session):
        self.command("state " + session)
        return json.loads(self.line(self.child.stdout, self.output))

    def idle(self, session):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = self.state(session)
            if state is not None and not state["running"]:
                return state
            time.sleep(0.005)
        raise AssertionError("Session did not join its run")

    def connect(self, restricted=False):
        host = self.url.hostname
        if host == "0.0.0.0":
            host = "127.0.0.1"
        sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
        sock.settimeout(5)
        if restricted:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.connect((host, self.url.port))
        wire = Wire(sock)
        self.wires.append(wire)
        return wire

    def open(self, method, path, body=b"", headers=None, restricted=False):
        headers = list(headers or [])
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            headers.append(("Content-Type", "application/json"))
        if self.token is not None:
            headers.append(("Authorization", "Bearer " + self.token))
        wire = self.connect(restricted)
        head = f"{method} {path} HTTP/1.1\r\nHost: fixture\r\nContent-Length: {len(body)}\r\n"
        head += "".join(f"{key}: {value}\r\n" for key, value in headers)
        wire.socket.sendall(head.encode() + b"\r\n" + body)
        return wire

    def request(self, method, path, body=b"", headers=None):
        wire = self.open(method, path, body, headers)
        try:
            return wire.response()
        finally:
            wire.close()

    def create(self):
        status, _, body = self.request("POST", "/sessions")
        if status != 201:
            raise AssertionError(body)
        return json.loads(body)["sessionId"]

    def prompt(self, session, message):
        wire = self.open("POST", f"/sessions/{session}/prompts", {"message": message})
        status, headers = wire.head()
        if status != 200:
            raise AssertionError(f"Prompt returned {status}")
        if headers["content-type"] != "application/jsonl; charset=utf-8":
            raise AssertionError("Wrong JSONL content type")
        values = wire.events()
        wire.close()
        self.idle(session)
        return values

    def finish(self):
        if self.child.stdin is not None:
            self.child.stdin.close()
            self.child.stdin = None
        output, diagnostics = self.child.communicate(timeout=5)
        diagnostics = bytes(self.diagnostic_log) + diagnostics
        if self.child.returncode != 0 or b"error(gpa)" in diagnostics:
            raise AssertionError(diagnostics.decode(errors="replace"))
        if self.output or output:
            raise AssertionError("Unexpected fixture output")


class HttpTests(unittest.TestCase):
    def test_routes_order_unicode_history_and_no_cors(self):
        with Server() as server:
            self.assertEqual("127.0.0.1", server.url.hostname)
            status, headers, body = server.request("GET", "/health")
            self.assertEqual(200, status)
            self.assertEqual({"status": "ok", "protocolVersion": 1}, json.loads(body))
            self.assertFalse(any(key.startswith("access-control-") for key in headers))
            session = server.create()
            self.assertRegex(session, r"^s_[0-9a-f]{32}$")
            values = server.prompt(session, 'café\n"quoted" 🙂')
            self.assertEqual(["agent_start", "text_delta", "agent_end"], [v["type"] for v in values])
            self.assertEqual([1, 2, 3], [v["sequence"] for v in values])
            self.assertEqual(1, len({v["runId"] for v in values}))
            self.assertRegex(values[0]["runId"], r"^r_[0-9a-f]{32}$")
            self.assertEqual('café\n"quoted" 🙂', values[-1]["text"])
            self.assertEqual("3", server.prompt(session, "history")[-1]["text"])
            _, _, body = server.request("POST", f"/sessions/{session}/cancel")
            self.assertEqual({"accepted": False, "runId": None}, json.loads(body))
            self.assertEqual(204, server.request("DELETE", f"/sessions/{session}")[0])
            self.assertEqual(404, server.request("POST", f"/sessions/{session}/prompts", {"message": "gone"})[0])
            self.assertEqual(404, server.request("GET", "/unknown")[0])
            server.finish()

    def test_authentication_ipv6_and_remote_options(self):
        for host in ("127.0.0.1", "::1"):
            with self.subTest(host=host), Server(host, "secret") as server:
                for authorization in (
                    "", "Authorization: Bearer wrong\r\n",
                    "Authorization: Bearer secret\r\nAuthorization: Bearer secret\r\n",
                ):
                    wire = server.connect()
                    wire.socket.sendall(f"GET /health HTTP/1.1\r\nHost: fixture\r\n{authorization}\r\n".encode())
                    status, headers, _ = wire.response()
                    self.assertEqual(401, status)
                    self.assertEqual("Bearer", headers["www-authenticate"])
                    wire.close()
                self.assertEqual(200, server.request("GET", "/health")[0])
                server.finish()
        for host, token, remote in (
            ("0.0.0.0", None, False), ("0.0.0.0", "secret", False),
            ("::", None, True), ("::100:0:0:1", None, False),
            ("127.0.0.1", "", False), ("127.0.0.1", "bad\n", False),
        ):
            with self.subTest(host=host, token=token, remote=remote), Server(host, token, remote) as server:
                self.assertEqual("error:" + ("InvalidToken" if host == "127.0.0.1" else "RemoteExposureNotAllowed"), server.startup)
                server.finish()
        with Server("0.0.0.0", "secret", True) as server:
            self.assertEqual(200, server.request("GET", "/health")[0])
            server.finish()

    def test_body_validation_and_exact_byte_limit(self):
        with Server() as server:
            session = server.create()
            path = f"/sessions/{session}/prompts"
            for body, headers, expected in (
                (b"{}", [], 415),
                (b"{}", [("Content-Type", "text/plain")], 415),
                (b"{", [("Content-Type", "application/json")], 400),
                (b"\xff", [("Content-Type", "application/json")], 400),
                (b"[]", [("Content-Type", "application/json")], 400),
                ({}, [], 400), ({"message": ""}, [], 400), ({"message": 42}, [], 400),
            ):
                self.assertEqual(expected, server.request("POST", path, body, headers)[0])
            wire = server.connect()
            wire.socket.sendall(f"POST {path} HTTP/1.1\r\nHost: fixture\r\nContent-Length: 1048577\r\n\r\n".encode())
            self.assertEqual(413, wire.response()[0])
            prefix, suffix = b'{"message":"ok","pad":"', b'"}'
            body = prefix + b"x" * (LIMIT - len(prefix) - len(suffix)) + suffix
            self.assertEqual(200, server.request("POST", path, body, [("Content-Type", "Application/JSON; charset=\"UTF-8\"")])[0])
            self.assertEqual(2, server.idle(session)["messages"])
            server.finish()

    def test_native_chunked_framing_and_malformed_headers(self):
        with Server() as server:
            session = server.create()
            path = f"/sessions/{session}/prompts"
            wire = server.connect()
            wire.socket.sendall((
                f"POST {path} HTTP/1.1\r\nHost: fixture\r\n"
                "Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
            ).encode())
            for byte in json.dumps({"message": "café"}, ensure_ascii=False).encode():
                wire.socket.sendall(b"1\r\n" + bytes([byte]) + b"\r\n")
            wire.socket.sendall(b"0\r\n\r\nGET /health HTTP/1.1\r\nHost: fixture\r\n\r\n")
            self.assertEqual(200, wire.head()[0])
            self.assertEqual("café", wire.events()[-1]["text"])
            server.idle(session)
            for headers in (
                "Content-Length: nope", "Content-Length: +1",
                "Content-Length: 1\r\nContent-Length: 2",
                "Content-Length: 1\r\nTransfer-Encoding: chunked",
                "Transfer-Encoding: chunked\r\nTransfer-Encoding: chunked",
                "Bad Header: x", "missing-colon",
            ):
                wire = server.connect()
                wire.socket.sendall(f"POST /sessions HTTP/1.1\r\nHost: fixture\r\n{headers}\r\n\r\n".encode())
                self.assertEqual(400, wire.response()[0], headers)
                wire.close()
            wire = server.connect()
            wire.socket.sendall(
                b"POST /sessions HTTP/1.1\r\nHost: fixture\r\nContent-Type: application/json\r\n"
                b"Content-Length: 2\r\nExpect: 100-Continue\r\n\r\n"
            )
            self.assertEqual(100, wire.head()[0])
            wire.socket.sendall(b"{}")
            self.assertEqual(201, wire.response()[0])
            server.finish()

    def test_provider_failure_and_oversized_events_roll_back(self):
        with Server() as server:
            session = server.create()
            server.prompt(session, "first")
            for message in ("fail", "oversize", "oversize_final"):
                values = server.prompt(session, message)
                self.assertEqual("failed", values[-1]["status"])
                self.assertEqual("provider_error" if message == "fail" else "output_too_large", values[-1]["error"]["code"])
                self.assertEqual(list(range(1, len(values) + 1)), [v["sequence"] for v in values])
                self.assertEqual(1, sum(v["type"] == "agent_end" for v in values))
                self.assertEqual(2, server.state(session)["messages"])
            self.assertEqual("3", server.prompt(session, "history")[-1]["text"])
            server.finish()

    def test_busy_cancel_and_independent_sessions_with_held_cleanup(self):
        with Server() as server:
            session, other = server.create(), server.create()
            path = f"/sessions/{session}"
            wire = server.open("POST", path + "/prompts", {"message": "hold"})
            self.assertEqual(200, wire.head()[0])
            started = json.loads(wire.line())
            self.assertEqual("waiting", json.loads(wire.line())["delta"])
            self.assertEqual(409, server.request("POST", path + "/prompts", {"message": "busy"})[0])
            self.assertEqual("completed", server.prompt(other, "independent")[-1]["status"])
            status, _, body = server.request("POST", path + "/cancel")
            self.assertEqual(200, status)
            self.assertEqual({"accepted": True, "runId": started["runId"]}, json.loads(body))
            self.assertEqual(409, server.request("POST", path + "/prompts", {"message": "still busy"})[0])
            self.assertEqual({"running": True, "messages": 0}, server.state(session))
            server.command("release")
            self.assertEqual("aborted", wire.events()[-1]["status"])
            self.assertEqual(0, server.idle(session)["messages"])
            server.finish()

    def test_silent_disconnect_cancels_only_its_run(self):
        with Server() as server:
            session = server.create()
            wire = server.open("POST", f"/sessions/{session}/prompts", {"message": "silent"})
            self.assertEqual(200, wire.head()[0])
            self.assertEqual("agent_start", json.loads(wire.line())["type"])
            server.marker("waiting")
            wire.close()
            self.assertEqual(0, server.idle(session)["messages"])
            self.assertEqual("1", server.prompt(session, "history")[-1]["text"])
            server.finish()

    def test_deletion_waits_for_cleanup_and_shutdown_joins_deleted_sessions(self):
        for stop in (False, True):
            with self.subTest(stop=stop), Server() as server:
                session = server.create()
                path = f"/sessions/{session}"
                prompt = server.open("POST", path + "/prompts", {"message": "hold"})
                prompt.head()
                server.marker("waiting")
                deletion = server.open("DELETE", path)
                server.marker("cleanup")
                self.assertEqual(404, server.request("POST", path + "/cancel")[0])
                self.assertFalse(select.select([deletion.socket], [], [], 0.1)[0])
                if stop:
                    server.command("stop")
                    self.assertIsNone(server.child.poll())
                server.command("release")
                if not stop:
                    self.assertEqual(204, deletion.response()[0])
                self.assertEqual("aborted", prompt.events()[-1]["status"])
                server.finish()

    def test_cancel_body_after_deletion_is_not_a_live_session(self):
        with Server() as server:
            session = server.create()
            wire = server.connect()
            wire.socket.sendall((
                f"POST /sessions/{session}/cancel HTTP/1.1\r\nHost: fixture\r\n"
                "Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{"
            ).encode())
            deadline = time.monotonic() + 5
            while True:
                server.command("references " + session)
                if json.loads(server.line(server.child.stdout, server.output)) == 2:
                    break
                self.assertLess(time.monotonic(), deadline, "Cancel request did not acquire its session")
                time.sleep(0.005)
            self.assertEqual(204, server.request("DELETE", f"/sessions/{session}")[0])
            wire.socket.sendall(b"}")
            self.assertEqual(404, wire.response()[0])
            server.finish()

    def test_shutdown_closes_idle_and_pending_body_reads(self):
        with Server() as server:
            idle = server.connect()
            pending = server.connect()
            pending.socket.sendall(b"POST /sessions HTTP/1.1\r\nHost: fixture\r\nContent-Type: application/json\r\nContent-Length: 10\r\n\r\n{")
            server.finish()
            for wire in (idle, pending):
                try:
                    self.assertEqual(b"", wire.socket.recv(1))
                except ConnectionResetError:
                    pass  # Closing a socket with unread input can send RST.

    def test_callback_stop_does_not_wait_on_its_own_run(self):
        with Server() as server:
            session = server.create()
            wire = server.open("POST", f"/sessions/{session}/prompts", {"message": "stop"})
            wire.head()
            server.marker("callback stopped")
            self.assertEqual("aborted", wire.events()[-1]["status"])
            server.finish()

    def test_stalled_native_delta_and_terminal_writes_have_bounded_cancellation(self):
        for message in ("large", "final_large"):
            for disconnect in (False, True):
                with self.subTest(message=message, disconnect=disconnect), Server() as server:
                    session = server.create()
                    path = f"/sessions/{session}"
                    wire = server.open("POST", path + "/prompts", {"message": message}, restricted=True)
                    wire.head()
                    self.assertEqual("agent_start", json.loads(wire.line())["type"])
                    if not wire.buffer:
                        wire.buffer.extend(wire.socket.recv(1))
                    self.assertTrue(server.state(session)["running"])
                    if disconnect:
                        wire.close()
                    else:
                        self.assertTrue(json.loads(server.request("POST", path + "/cancel")[2])["accepted"])
                    self.assertEqual(0, server.idle(session)["messages"])
                    wire.close()
                    self.assertEqual("1", server.prompt(session, "history")[-1]["text"])
                    server.finish()

    def test_eof_after_successful_final_framing_preserves_commit(self):
        with Server() as server:
            session = server.create()
            wire = server.open("POST", f"/sessions/{session}/prompts", {"message": "pause_final"})
            wire.head()
            self.assertEqual("completed", wire.events()[-1]["status"])
            server.marker("framing delivered")
            self.assertTrue(server.state(session)["running"])
            wire.close()
            server.marker("disconnected")
            server.command("release")
            self.assertEqual(2, server.idle(session)["messages"])
            server.finish()


if __name__ == "__main__":
    unittest.main(verbosity=2)
