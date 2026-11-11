"""A scripted model server that speaks all three provider APIs and misbehaves on demand.

The reply to a request is a pure function of the conversation in it: the last
user message names a script (`[[name]]`), and the next turn is the first one
whose tool calls have no results yet. So a retried, resumed or repeated
request gets the same answer a real model would be expected to give, and a
target can be killed and restarted without the fake losing its place.
"""

from __future__ import annotations

import json
import re
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import wire
from wire import Call, Turn

MARK = re.compile(r"\[\[([\w.-]+)\]\]")
SUMMARY = "Summary: the user ran scripted steps and they finished."

# Every misbehaviour the server knows. `expect` is what the specification
# requires of the run: "completed", "failed", or "any" terminal state.
FAULTS: dict[str, dict[str, Any]] = {
    "http429":             {"status": 429, "expect": "completed", "retried": True},
    "http429_retry_after": {"status": 429, "retry_after": "0", "expect": "completed", "retried": True},
    "http500":             {"status": 500, "retry_after": "0", "expect": "completed", "retried": True},
    "http503":             {"status": 503, "expect": "completed", "retried": True},
    "http503_retry_after": {"status": 503, "retry_after": "0", "expect": "completed", "retried": True},
    "http401":             {"status": 401, "expect": "failed", "retried": False},
    "reset":               {"expect": "completed", "retried": True},
    "drop":                {"expect": "failed", "retried": False},
    "truncated":           {"expect": "failed", "retried": False},
    "error_event":         {"expect": "failed", "retried": False},
    "stall_headers":       {"expect": "completed", "retried": False},
    "stall_mid":           {"expect": "completed", "retried": False},
    "split_utf8":          {"expect": "completed", "retried": False},
    "fragments":           {"expect": "completed", "retried": False},
    "unknown_tool":        {"expect": "completed", "retried": False},
    "malformed":           {"expect": "any"},
    "duplicated":          {"expect": "any"},
    "reordered":           {"expect": "any"},
    "oversized":           {"expect": "any"},
    "bad_args":            {"expect": "any"},
}


@dataclass
class Fault:
    kind: str
    status: int = 0
    retry_after: str | None = None
    seconds: float = 0.3          # how long a stall lasts unless released sooner
    after: int = 1                # stall_mid / drop: events sent first

    @classmethod
    def named(cls, name: str, **changes: Any) -> "Fault":
        known = FAULTS[name]
        kind = "status" if "status" in known else name
        return cls(kind, known.get("status", 0), known.get("retry_after"), **changes)


@dataclass
class Request:
    number: int
    wire: str
    headers: dict[str, str]
    body: dict
    items: list[dict]
    problems: list[str]
    script: str | None
    turn: int | None
    fault: str | None
    at: float = field(default_factory=time.monotonic)


class FakeProvider:
    def __init__(self, think: float = 0.0) -> None:
        self.think = think                      # model latency before each reply, in seconds
        self.requests: list[Request] = []
        self.scripts: dict[str, list[Turn]] = {}
        self.release = threading.Event()        # set to end every stall
        self.stalled = threading.Event()        # set while a request is stalling
        self._faults: dict[int, Fault] = {}
        self._always: Fault | None = None
        self._lock = threading.Condition()
        self._names = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                                        daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def script(self, *turns: Turn, name: str | None = None) -> str:
        """Register a script and return the prompt that selects it."""
        with self._lock:
            self._names += 1
            name = name or f"s{self._names}"
            for number, turn in enumerate(turns):
                for index, call in enumerate(turn.calls):
                    call.id = f"call_{name}_{number}_{index}"
            self.scripts[name] = list(turns)
        return f"[[{name}]] follow the script"

    def fault(self, number: int, fault: Fault) -> None:
        """Misbehave on the request with this 1-based number."""
        self._faults[number] = fault

    def fault_next(self, fault: Fault) -> None:
        self._faults[len(self.requests) + 1] = fault

    def fault_always(self, fault: Fault | None) -> None:
        self._always = fault

    def wait_requests(self, count: int, timeout: float = 20.0) -> None:
        with self._lock:
            if not self._lock.wait_for(lambda: len(self.requests) >= count, timeout):
                raise TimeoutError(f"provider saw {len(self.requests)} requests, expected {count}")

    def close(self) -> None:
        self.release.set()
        self._server.shutdown()
        self._server.server_close()

    # -- used by the handler ------------------------------------------------

    def _record(self, kind: str, headers: dict[str, str], body: dict) -> tuple[Request, Turn, Fault | None]:
        sequence = wire.items(kind, body)
        last_user = max((index for index, item in enumerate(sequence) if item["kind"] == "user"), default=-1)
        match = MARK.search(sequence[last_user]["text"]) if last_user >= 0 else None
        name = match.group(1) if match else None
        answered = {item["id"] for item in sequence[last_user + 1:] if item["kind"] == "result"}
        turn, index = Turn(text="ok"), None
        if not wire.has_tools(body):
            turn, name = Turn(text=SUMMARY), None      # only a compaction summary request has no tools
        elif name in self.scripts:
            turns = self.scripts[name]
            index = next((number for number, candidate in enumerate(turns)
                          if not candidate.calls or not all(call.id in answered for call in candidate.calls)),
                         len(turns) - 1)
            turn = turns[index]
        with self._lock:
            number = len(self.requests) + 1
            fault = self._faults.pop(number, None) or self._always
            request = Request(number, kind, headers, body, sequence, wire.problems(kind, body), name, index,
                              fault.kind if fault else None)
            self.requests.append(request)
            self._lock.notify_all()
        if fault and fault.kind == "unknown_tool":
            turn = Turn(calls=[Call("true", name="no_such_tool", id=f"call_fault_{number}")])
        elif fault and fault.kind == "bad_args":
            turn = Turn(calls=[Call(raw='{"command": "echo', id=f"call_fault_{number}")])
        return request, turn, fault

    def _stall(self, seconds: float) -> None:
        self.stalled.set()
        self.release.wait(seconds)
        self.stalled.clear()


def _split_utf8(data: bytes) -> list[bytes]:
    """Cut in the middle of every multi-byte character."""
    cuts = [index for index in range(1, len(data)) if data[index] & 0xC0 == 0x80]
    return [data[start:end] for start, end in zip([0] + cuts, cuts + [len(data)])]


def _handler(provider: FakeProvider) -> type:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *arguments: Any) -> None:
            pass

        def do_POST(self) -> None:
            self.close_connection = True
            try:
                self._reply()
            except OSError:
                pass     # the target went away mid-reply; that is its right

        def _reply(self) -> None:
            kind = next((name for suffix, name in wire.PATHS.items() if self.path.split("?")[0].endswith(suffix)), None)
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            if kind is None or not isinstance(body, dict):
                return self._json(404 if kind is None else 400, {"error": {"message": "bad request to the fake"}})
            headers = {name.lower(): value for name, value in self.headers.items()}
            request, turn, fault = provider._record(kind, headers, body)
            if provider.think:
                time.sleep(provider.think)
            what = fault.kind if fault else None
            if what == "status":
                extra = {} if fault.retry_after is None else {"Retry-After": fault.retry_after}
                return self._json(fault.status, {"error": {"message": f"injected {fault.status}",
                                                           "type": "injected_error"}}, extra)
            if what == "reset":
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                return self.connection.close()
            if what == "stall_headers":
                provider._stall(fault.seconds)
            events = wire.encode(kind, turn, model=str(body.get("model")),
                                 fragment=1 if what == "fragments" else 0)
            if what == "malformed":
                events.insert(1, b"data: {\"type\": not json at all\n\n")
            elif what == "duplicated":
                events = [event for event in events for _ in range(2)]
            elif what == "reordered":
                events.reverse()
            elif what == "oversized":
                events.insert(1, wire.oversized(kind))
            elif what == "error_event":
                events[1:] = [wire.error_event(kind)]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if what == "truncated":
                data = b"".join(events)
                return self._chunk(data[:len(data) * 2 // 3])     # then the connection closes mid-event
            for index, event in enumerate(events):
                if what in ("stall_mid", "drop") and index == min(fault.after, len(events) - 1):
                    if what == "drop":
                        return                                    # no terminating chunk
                    provider._stall(fault.seconds)
                if what == "split_utf8":
                    for part in _split_utf8(event):
                        self._chunk(part)
                        time.sleep(0.001)                         # keeps the halves in separate TCP segments
                else:
                    self._chunk(event)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        def _chunk(self, data: bytes) -> None:
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        def _json(self, status: int, value: dict, extra: dict[str, str] | None = None) -> None:
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            for name, text in (extra or {}).items():
                self.send_header(name, text)
            self.end_headers()
            self.wfile.write(data)

    return Handler
