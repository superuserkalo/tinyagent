"""Process control and the two wire clients (RPC over stdio, HTTP).

`RpcLink` and `HttpLink` expose the same methods, so a scenario written
against a "link" runs over either transport. Every blocking call has a
deadline and raises `Hang` when it passes.
"""

from __future__ import annotations

import http.client
import json
import os
import queue
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

DEADLINE = 15.0     # generous: no single step of a scenario should take a tenth of this
POLL = 0.002
TERMINAL = ("agent_end", "compaction_end")


class Hang(AssertionError):
    """A step passed its deadline (invariant 5)."""


class TargetDied(Exception):
    """The target process went away while a reply was awaited."""


class CommandError(Exception):
    def __init__(self, code: str, message: str = "", status: int | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.status = code, message, status


def wait_until(predicate: Callable[[], Any], timeout: float = DEADLINE, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise Hang(f"timed out after {timeout:g} s waiting for {what}")
        time.sleep(POLL)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def group_alive(pgid: int) -> bool:
    """True while any process is left in the group this PID led."""
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


class Process:
    """One target process in its own process group, with its output drained."""

    live: set["Process"] = set()

    def __init__(self, argv: list[str], env: dict[str, str], cwd: str,
                 on_line: Callable[[bytes | None], None] | None = None) -> None:
        self.argv = argv
        self.popen = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=env, cwd=cwd, start_new_session=True)
        self.pid = self.popen.pid
        self.stdout = bytearray()
        self.stderr = bytearray()
        self._on_line = on_line
        Process.live.add(self)
        self._threads = [threading.Thread(target=self._drain_out, daemon=True),
                         threading.Thread(target=self._drain_err, daemon=True)]
        for thread in self._threads:
            thread.start()

    def _drain_out(self) -> None:
        for line in self.popen.stdout:
            self.stdout += line
            if self._on_line:
                self._on_line(line)
        if self._on_line:
            self._on_line(None)

    def _drain_err(self) -> None:
        for line in self.popen.stderr:
            self.stderr += line

    @property
    def alive(self) -> bool:
        return self.popen.poll() is None

    def write(self, data: bytes) -> None:
        try:
            self.popen.stdin.write(data)
            self.popen.stdin.flush()
        except (OSError, ValueError) as error:
            raise TargetDied(f"cannot write to the target: {error}") from error

    def close_stdin(self) -> None:
        try:
            self.popen.stdin.close()
        except OSError:
            pass

    def signal(self, number: int) -> None:
        try:
            os.kill(self.pid, number)
        except ProcessLookupError:
            pass

    def kill(self) -> None:
        self.signal(signal.SIGKILL)
        self.wait(5)

    def wait(self, timeout: float = DEADLINE) -> int:
        try:
            code = self.popen.wait(timeout)
        except subprocess.TimeoutExpired:
            raise Hang(f"target did not exit within {timeout:g} s: {' '.join(self.argv[:3])}") from None
        for thread in self._threads:
            thread.join(2)
        return code

    def group_alive(self) -> bool:
        return group_alive(self.pid)

    def destroy(self) -> None:
        """Last resort for cleanup: kill the whole group, never raise."""
        for target in (lambda: os.killpg(self.pid, signal.SIGKILL), lambda: self.popen.kill()):
            try:
                target()
            except OSError:
                pass
        try:
            self.popen.wait(5)
        except subprocess.TimeoutExpired:
            pass
        for stream in (self.popen.stdin, self.popen.stdout, self.popen.stderr):
            try:
                stream.close()
            except OSError:
                pass
        Process.live.discard(self)


def record_count(directory: str) -> int:
    """Complete journal records in the session directory (newline-terminated JSONL lines)."""
    total = 0
    for path in Path(directory).glob("*.jsonl"):
        try:
            total = max(total, path.read_bytes().count(b"\n"))
        except OSError:
            pass
    return total


class KillAt(threading.Thread):
    """SIGKILL the process as soon as the journal holds `at` records."""

    def __init__(self, process: Process, directory: str, at: int) -> None:
        super().__init__(daemon=True)
        self.process, self.directory, self.at = process, directory, at
        self.killed = False
        self.stop = threading.Event()
        self.start()

    def run(self) -> None:
        while not self.stop.is_set() and self.process.alive:
            if record_count(self.directory) >= self.at:
                self.process.signal(signal.SIGKILL)
                self.killed = True
                return
            time.sleep(0.0003)

    def finish(self, grace: float = 2.0) -> bool:
        """Stop watching; True if the kill happened. The last records may land just after agent_end."""
        self.join(grace)
        self.stop.set()
        self.join(5)
        return self.killed


class Run:
    """The event stream of one prompt, resume or compaction."""

    def __init__(self, read: Callable[[float], dict | None], data: dict | None = None,
                 close: Callable[[], None] | None = None) -> None:
        self._read, self._close = read, close
        self.data = data or {}
        self.events: list[dict] = []
        self.end: dict | None = None
        self.died = False             # the stream ended without a terminal event
        self.partial = False          # set by callers that cut the stream on purpose

    def next(self, timeout: float = DEADLINE) -> dict | None:
        """The next event, or None once the stream is over."""
        if self.end is not None or self.died:
            return None
        try:
            event = self._read(timeout)
        except TargetDied:
            event = None
        if event is None:
            self.died = True
            return None
        self.events.append(event)
        if event.get("type") in TERMINAL:
            self.end = event
        return event

    def take(self, count: int, timeout: float = DEADLINE) -> "Run":
        deadline = time.monotonic() + timeout
        while len(self.events) < count and self.next(max(0.01, deadline - time.monotonic())):
            pass
        return self

    def until(self, kind: str, timeout: float = DEADLINE) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            event = self.next(max(0.01, deadline - time.monotonic()))
            if event is None:
                raise AssertionError(f"stream ended before {kind}: {self.status}")
            if event.get("type") == kind:
                return event

    def drain(self, timeout: float = DEADLINE) -> "Run":
        deadline = time.monotonic() + timeout
        while self.next(max(0.01, deadline - time.monotonic())):
            pass
        return self

    def disconnect(self) -> None:
        self.partial = True
        if self._close:
            self._close()

    @property
    def status(self) -> str:
        if self.end is None:
            return "died" if self.died else "running"
        status = self.end.get("status")
        return f"failed:{(self.end.get('error') or {}).get('code')}" if status == "failed" else str(status)

    def of(self, kind: str) -> list[dict]:
        return [event for event in self.events if event.get("type") == kind]

    @property
    def text(self) -> str:
        return "".join(event.get("delta", "") for event in self.of("text_delta"))


class RpcLink:
    transport = "rpc"

    def __init__(self, process_factory: Callable[[Callable], Process], on_run: Callable[[Run], None]) -> None:
        self._events: queue.Queue = queue.Queue()
        self._responses: dict[str, dict] = {}
        self._arrived = threading.Condition()
        self._ids = 0
        self._on_run = on_run
        self.log: list[dict] = []           # everything the target wrote, in order
        self.garbage: list[bytes] = []      # stdout lines that were not JSON objects
        self.eof = False
        self.process = process_factory(self._line)

    def _line(self, line: bytes | None) -> None:
        with self._arrived:
            if line is None:
                self.eof = True
                self._events.put(None)
            else:
                try:
                    value = json.loads(line)
                    assert isinstance(value, dict) and line.endswith(b"\n")
                except (ValueError, AssertionError):
                    self.garbage.append(line)
                    return
                self.log.append(value)
                if value.get("type") == "response" and "id" in value:
                    self._responses[value["id"]] = value
                else:
                    self._events.put(value)
            self._arrived.notify_all()

    def send(self, command: dict) -> None:
        self.process.write(json.dumps(command, ensure_ascii=False).encode() + b"\n")

    def call(self, kind: str, timeout: float = DEADLINE, **fields: Any) -> dict:
        """Send one command and return its response data, or raise CommandError."""
        self._ids += 1
        identifier = f"h{self._ids}"
        self.send({"id": identifier, "type": kind, **{k: v for k, v in fields.items() if v is not None}})
        with self._arrived:
            if not self._arrived.wait_for(lambda: identifier in self._responses or self.eof, timeout):
                raise Hang(f"no response to {kind} within {timeout:g} s")
            response = self._responses.get(identifier)
        if response is None:
            raise TargetDied(f"target exited before answering {kind}")
        if not response.get("success"):
            error = response.get("error") or {}
            raise CommandError(str(error.get("code")), str(error.get("message")))
        return response.get("data") or {}

    def event(self, timeout: float = DEADLINE) -> dict | None:
        """The next non-response line (run event or protocol_error); None at EOF."""
        try:
            value = self._events.get(timeout=timeout)
        except queue.Empty:
            raise Hang(f"no event within {timeout:g} s") from None
        if value is None:
            self._events.put(None)
        return value

    def _run(self, kind: str, **fields: Any) -> Run:
        run = Run(self.event, self.call(kind, **fields))
        self._on_run(run)
        return run

    def prompt(self, message: Any, message_id: str | None = None, detach: bool = False) -> Run:
        return self._run("prompt", message=message, messageId=message_id)

    def resume(self, result: Any = None, error: str | None = None, detach: bool = False) -> Run:
        return self._run("resume_run", result=result, error=error)

    def compact(self) -> Run:
        return self._run("compact")

    def abort(self) -> dict:
        return self.call("abort")

    def abandon(self, reason: str | None = None) -> dict:
        return self.call("abandon_run", reason=reason)

    def reconcile(self, note: str) -> dict:
        return self.call("reconcile", note=note)

    def history(self, view: str = "history", offset: int = 0, limit: int = 1000) -> dict:
        return self.call("get_history", view=view, offset=offset, limit=limit)

    def events(self, after: int = 0, limit: int = 100) -> dict:
        return self.call("get_events", after=after, limit=limit)

    def state(self) -> dict:
        """get_state, plus recoveryRequired from the history page."""
        state = self.call("get_state")
        if state.get("status") == "idle":
            state["recoveryRequired"] = bool(self.history(limit=1).get("recoveryRequired"))
        state["running"] = state.get("status") == "running"
        return state

    @property
    def session_id(self) -> str:
        return self.history(limit=1)["sessionId"]

    def close(self, timeout: float = DEADLINE) -> int:
        """EOF on stdin, which must cancel any run and end the process."""
        self.process.close_stdin()
        return self.process.wait(timeout)

    def kill(self) -> None:
        self.process.kill()


class HttpServer:
    def __init__(self, process: Process, port: int, token: str | None = None) -> None:
        self.process, self.port, self.token = process, port, token

    def request(self, method: str, path: str, body: Any = None, *, headers: dict | None = None,
                raw: bytes | None = None, timeout: float = DEADLINE) -> tuple[int, Any]:
        connection, response = self._send(method, path, body, headers, raw, timeout)
        try:
            data = response.read()
        except socket.timeout:
            raise Hang(f"{method} {path} did not finish within {timeout:g} s") from None
        except (OSError, http.client.HTTPException) as error:
            raise TargetDied(f"{method} {path}: {error}") from error
        finally:
            connection.close()
        try:
            return response.status, json.loads(data) if data else None
        except ValueError:
            return response.status, data

    def _send(self, method: str, path: str, body: Any, headers: dict | None, raw: bytes | None,
              timeout: float) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        send = {} if self.token is None else {"Authorization": f"Bearer {self.token}"}
        data = raw
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
        if data is not None:
            send["Content-Type"] = "application/json"
        send.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            try:
                connection.request(method, path, body=data, headers=send)
            except (BrokenPipeError, ConnectionResetError):
                pass      # the server answered and closed before reading the whole body (413)
            if connection.sock is None:           # the reset closed the socket: there is no reply to read
                raise ConnectionResetError("connection closed before a response")
            connection.held = connection.sock     # getresponse() drops it for Connection: close
            return connection, connection.getresponse()
        except socket.timeout:
            connection.close()
            raise Hang(f"{method} {path} got no response within {timeout:g} s") from None
        except (OSError, http.client.HTTPException) as error:
            connection.close()
            raise TargetDied(f"{method} {path}: {error}") from error

    def call(self, method: str, path: str, body: Any = None, **options: Any) -> Any:
        status, value = self.request(method, path, body, **options)
        if status >= 400:
            error = value.get("error") if isinstance(value, dict) else None
            raise CommandError(str((error or {}).get("code")), str((error or {}).get("message")), status)
        return value

    def stream(self, method: str, path: str, body: Any = None) -> Run:
        """A JSONL response as a Run. Errors before the stream raise CommandError."""
        connection, response = self._send(method, path, body, None, None, DEADLINE)
        if response.status != 200:
            data = response.read()
            connection.close()
            try:
                error = json.loads(data).get("error") or {}
            except (ValueError, AttributeError):
                error = {}
            raise CommandError(str(error.get("code")), str(error.get("message")), response.status)

        def read(timeout: float) -> dict | None:
            try:
                connection.held.settimeout(timeout)
                line = response.readline()
            except socket.timeout:
                raise Hang(f"no event on {path} within {timeout:g} s") from None
            except (OSError, http.client.HTTPException, ValueError):
                return None
            if not line.endswith(b"\n"):
                return None
            event = json.loads(line)
            if event.get("type") in TERMINAL and not path.endswith("follow=1"):
                try:
                    trailing = response.read()   # the response must end here; then the run is over
                except socket.timeout:
                    raise Hang(f"terminal event on {path} was not framed within {timeout:g} s") from None
                except (OSError, http.client.HTTPException) as error:
                    # A killed server can die between the terminal chunk and its
                    # final framing. Report a dead stream, not a completed run.
                    raise TargetDied(f"terminal framing on {path}: {error}") from error
                if trailing.strip():
                    event["trailingBytes"] = len(trailing)
            return event

        def close() -> None:
            try:
                connection.held.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            response.close()
            connection.close()

        run = Run(read, {"contentType": response.getheader("Content-Type")}, close)
        return run

    def stop(self, timeout: float = DEADLINE) -> int:
        self.process.signal(signal.SIGTERM)
        return self.process.wait(timeout)


class HttpLink:
    transport = "http"

    def __init__(self, server: HttpServer, on_run: Callable[[Run], None], session_id: str | None = None) -> None:
        self.server, self._on_run = server, on_run
        self.process = server.process
        self.session_id = server.call("POST", "/sessions", {"sessionId": session_id} if session_id else None)["sessionId"]
        self.path = f"/sessions/{self.session_id}"

    def _run(self, action: str, body: dict) -> Run:
        run = self.server.stream("POST", f"{self.path}/{action}", {k: v for k, v in body.items() if v is not None})
        self._on_run(run)
        return run

    def prompt(self, message: Any, message_id: str | None = None, detach: bool = False) -> Run:
        return self._run("prompts", {"message": message, "messageId": message_id, "detach": detach or None})

    def resume(self, result: Any = None, error: str | None = None, detach: bool = False) -> Run:
        return self._run("resume", {"result": result, "error": error, "detach": detach or None})

    def compact(self) -> Run:
        return self._run("compact", {})

    def follow(self, after: int = 0) -> Run:
        run = self.server.stream("GET", f"{self.path}/events?after={after}&follow=1")
        run.partial = True      # a follow stream is not one whole run; checked by its own rules
        return run

    def abort(self) -> dict:
        return self.server.call("POST", f"{self.path}/cancel")

    def abandon(self, reason: str | None = None) -> dict:
        return self.server.call("POST", f"{self.path}/abandon", {"reason": reason} if reason else None)

    def reconcile(self, note: str) -> dict:
        return self.server.call("POST", f"{self.path}/reconcile", {"note": note})

    def history(self, view: str = "history", offset: int = 0, limit: int = 1000) -> dict:
        return self.server.call("GET", f"{self.path}?offset={offset}&limit={limit}&view={view}")

    def events(self, after: int = 0, limit: int = 100) -> dict:
        return self.server.call("GET", f"{self.path}/events?after={after}&limit={limit}")

    def state(self) -> dict:
        """The session page; `running` is unknown over HTTP, so wait_idle() is used instead."""
        page = self.history(limit=1)
        return {"resumable": page.get("resumable"), "recoveryRequired": page.get("recoveryRequired"),
                "paused": page.get("paused"), "running": None}

    def wait_idle(self, timeout: float = DEADLINE) -> None:
        """A follow stream ends when no run is active."""
        self.follow(10 ** 9).drain(timeout)

    def close(self, timeout: float = DEADLINE) -> int:
        status, _ = self.server.request("DELETE", self.path, timeout=timeout)
        return 0 if status == 204 else status

    def kill(self) -> None:
        self.process.kill()


def lines(data: Iterable[bytes] | bytes) -> list[str]:
    text = bytes(data).decode("utf-8", "replace")
    return [line for line in text.splitlines() if line]
