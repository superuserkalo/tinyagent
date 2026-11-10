"""Threaded HTTP transport for one configured native Agent."""

from __future__ import annotations

import hmac
import ipaddress
import json
import re
import secrets
import select
import socket
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Callable
from urllib.parse import parse_qs, urlsplit

from tinyagent_http_body import HttpError, read_body
from tinyagent_session import Session, ContextBudget, RecoveryRequiredError
from tinyagent_storage import SessionBusyError, SessionCorruptionError
from tinyagent_types import (
    AgentEvent,
    CancellationToken,
    JsonObject,
    MAX_JSONL_LINE_BYTES,
    PROTOCOL_VERSION,
)

if TYPE_CHECKING:
    from tiny_agent import Agent


@dataclass
class _Run:
    connection: socket.socket
    run_id: str = field(default_factory=lambda: f"r_{secrets.token_hex(16)}")
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    done: threading.Event = field(default_factory=threading.Event)


@dataclass
class _Session:
    core: Session
    active: _Run | None = None
    deleted: bool = False

    @property
    def history(self) -> list[JsonObject]:
        return self.core.history


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, owner: _Application) -> None:
        self.owner = owner
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        super().__init__(address, handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(30)
        with self.owner._lock:
            self.owner._connections.add(connection)
        return connection, address

    def close_request(self, request) -> None:
        with self.owner._lock:
            self.owner._connections.discard(request)
        super().close_request(request)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TinyAgent/1"
    sys_version = ""
    server: _Server
    _response_started = False

    def log_message(self, format: str, *args: object) -> None:
        # Protocol bodies and bearer tokens never enter an access log.
        pass

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionError, TimeoutError):
            pass

    def end_headers(self) -> None:
        self._response_started = True
        super().end_headers()

    def _dispatch(self) -> None:
        self.close_connection = True
        try:
            self.server.owner._route(self)
        except (BrokenPipeError, ConnectionError, TimeoutError):
            pass
        except Exception as error:
            if self._response_started:
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return
            if isinstance(error, HttpError):
                failure = error
            elif isinstance(error, SessionBusyError):
                failure = HttpError(409, "session_busy", str(error))
            elif isinstance(error, RecoveryRequiredError):
                failure = HttpError(409, "recovery_required", str(error))
            elif isinstance(error, SessionCorruptionError):
                failure = HttpError(409, "session_corrupt", str(error))
            elif isinstance(error, FileNotFoundError):
                failure = HttpError(404, "session_not_found", "Unknown session")
            elif isinstance(error, ValueError):
                failure = HttpError(400, "invalid_request", str(error))
            else:
                failure = HttpError(500, "internal_error", "Request failed")
            self._json(
                failure.status,
                {
                    "error": {
                        "code": failure.code,
                        "message": str(failure),
                        "retryable": failure.status >= 500,
                    }
                },
            )

    do_GET = do_POST = do_DELETE = do_OPTIONS = do_PUT = do_PATCH = do_HEAD = _dispatch

    def send_error(self, code, message=None, explain=None) -> None:
        self._json(
            code,
            {
                "error": {
                    "code": "invalid_request",
                    "message": message or "Invalid HTTP request",
                    "retryable": False,
                }
            },
        )

    def _json(self, status: int, value: JsonObject) -> None:
        payload = json.dumps(
            value, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)


class AgentServer:
    """A background HTTP server. close() waits for cooperative run cleanup."""

    def __init__(
        self,
        agent: Agent,
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        token: str | None = None,
        allow_remote: bool = False,
        directory: str | None = None,
        budget: ContextBudget | None = None,
    ) -> None:
        host = "127.0.0.1" if host == "localhost" else host
        try:
            address = ipaddress.ip_address(host)
        except ValueError as error:
            raise ValueError("host must be an IP address or localhost") from error
        if token is not None and not re.fullmatch(r"[\x21-\x7e]+", token):
            raise ValueError("token must contain non-space ASCII characters")
        if not address.is_loopback and not (allow_remote and token):
            raise ValueError("remote binding requires allow_remote and a bearer token")
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 0 <= port <= 65535
        ):
            raise ValueError("port must be an integer from 0 to 65535")
        self._application = _Application(agent, token, directory=directory, budget=budget)
        self._close_lock = threading.Lock()
        self._closed = False
        self._server = _Server((host, port), _Handler, self._application)
        actual_port = self._server.server_address[1]
        self.url = (
            f"http://{'[' + host + ']' if address.version == 6 else host}:{actual_port}"
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            try:
                self._server.shutdown()
                self._application.close()
                self._server.server_close()
                self._thread.join()
            finally:
                self._closed = True

    def __enter__(self) -> AgentServer:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class _Application:
    """HTTP sessions and request handling, independent of listener ownership."""

    def __init__(self, agent: Agent, token: str | None = None, *,
                 directory: str | None = None, budget: ContextBudget | None = None) -> None:
        self._agent = agent
        self._token = token
        self._directory = directory
        self._budget = budget
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        self._connections: set[socket.socket] = set()
        self._closing = False
        self._closed = threading.Event()

    def _route(self, request: _Handler) -> None:
        if self._token:
            credentials = request.headers.get_all("Authorization", [])
            supplied = credentials[0].encode("utf-8") if credentials else b""
            if len(credentials) != 1 or not hmac.compare_digest(
                supplied, f"Bearer {self._token}".encode("ascii")
            ):
                raise HttpError(401, "unauthorized", "A valid bearer token is required")
        with self._lock:
            if self._closing:
                raise HttpError(503, "server_closing", "Server is shutting down")
        path = request.path.split("?", 1)[0]
        method = request.command
        if method == "GET" and path == "/health":
            request._json(200, {"status": "ok", "protocolVersion": PROTOCOL_VERSION})
            return
        if method == "GET" and path == "/sessions":
            with self._lock:
                identifiers = set(self._sessions)
                if self._directory:
                    identifiers.update(Session.list(self._directory))
            request._json(200, {"sessionIds": sorted(identifiers)})
            return
        if method == "POST" and path == "/sessions":
            body = read_body(request, required=False) or {}
            identifier = body.get("sessionId")
            if identifier is not None and (not isinstance(identifier, str) or not self._directory):
                raise HttpError(400, "invalid_request", "resume requires storage and a string sessionId")
            with self._lock:
                if self._closing:
                    raise HttpError(503, "server_closing", "Server is shutting down")
                if identifier in self._sessions:
                    raise HttpError(409, "session_busy", "Session is already open")
                core = (Session.open(self._agent, self._directory, identifier, budget=self._budget)
                        if identifier else Session(self._agent, directory=self._directory, budget=self._budget))
                self._sessions[core.id] = _Session(core)
            request._json(201, {"sessionId": core.id})
            return
        match = re.fullmatch(r"/sessions/([^/]+)(?:/(prompts|cancel|compact|reconcile))?", path)
        if not match:
            raise HttpError(404, "not_found", "Unknown route")
        session_id, action = match.groups()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.deleted:
                raise HttpError(404, "session_not_found", "Unknown session")
        if method == "GET" and action is None:
            query = parse_qs(urlsplit(request.path).query)
            if any(len(values) != 1 for values in query.values()):
                raise HttpError(400, "invalid_request", "Duplicate inspection options")
            request._json(200, session.core.inspect(offset=int(query.get("offset", ["0"])[0]),
                          limit=int(query.get("limit", ["20"])[0]), view=query.get("view", ["history"])[0]))
        elif method == "POST" and action == "reconcile":
            body = read_body(request, required=True)
            with self._lock:
                if session.active:
                    raise HttpError(409, "session_busy", "Session has an active run")
                if session.deleted or self._closing:
                    raise HttpError(409, "session_closed", "Session is closing")
                session.core.reconcile(body.get("note"))
            request._json(200, {"recoveryRequired": False})
        elif method == "POST" and action in ("prompts", "compact"):
            body = read_body(request, required=action == "prompts")
            message = body.get("message") if body else None
            if action == "prompts" and (not isinstance(message, str) or not message):
                raise HttpError(
                    400, "invalid_request", "message must be a non-empty string"
                )
            if session.core.recovery_required:
                raise HttpError(409, "recovery_required", "Inspect history and reconcile interrupted tool effects")
            if action == "compact" and self._budget is None:
                raise HttpError(400, "budget_required", "Configure a compaction budget")
            with self._lock:
                if session.deleted:
                    raise HttpError(404, "session_not_found", "Unknown session")
                if self._closing:
                    raise HttpError(503, "server_closing", "Server is shutting down")
                busy = session.active is not None
                if not busy:
                    run = _Run(request.connection)
                    session.active = run
            if busy:
                request._json(
                    409,
                    {
                        "error": {
                            "code": "session_busy",
                            "message": "The session already has an active run",
                            "retryable": True,
                        }
                    },
                )
                return
            self._prompt(request, session, run, message, compact=action == "compact")
        elif method == "POST" and action == "cancel":
            read_body(request, required=False)
            with self._lock:
                if session.deleted:
                    raise HttpError(404, "session_not_found", "Unknown session")
                if self._closing:
                    raise HttpError(503, "server_closing", "Server is shutting down")
                run = session.active
            if run:
                run.cancellation.cancel()
            request._json(
                200, {"accepted": run is not None, "runId": run.run_id if run else None}
            )
        elif method == "DELETE" and action is None:
            with self._lock:
                session.deleted = True
                run = session.active
            if run:
                run.cancellation.cancel()
                run.done.wait()
            session.core.close()
            with self._lock:
                self._sessions.pop(session_id, None)
            request.send_response(204)
            request.send_header("Connection", "close")
            request.end_headers()
        else:
            raise HttpError(404, "not_found", "Unknown route")

    def _prompt(
        self, request: _Handler, session: _Session, run: _Run, message: str | None, *, compact: bool = False
    ) -> None:
        finished = threading.Event()

        def watch_disconnect() -> None:
            while not finished.wait(0.05):
                try:
                    readable, _, _ = select.select([request.connection], [], [], 0)
                    if readable and not request.connection.recv(1, socket.MSG_PEEK):
                        run.cancellation.cancel()
                        return
                except (OSError, ValueError):
                    run.cancellation.cancel()
                    return

        watcher = threading.Thread(target=watch_disconnect, daemon=True)

        def emit(event: AgentEvent) -> None:
            value = event.as_dict()
            data = json.dumps(
                value, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
            oversized = len(data) > MAX_JSONL_LINE_BYTES
            if oversized:
                run.cancellation.cancel()
                data = json.dumps(
                    {
                        "type": "agent_end",
                        "runId": event.run_id,
                        "sequence": event.sequence,
                        "status": "failed",
                        "error": {
                            "code": "output_too_large",
                            "message": "Event exceeds the JSONL byte limit",
                            "retryable": False,
                        },
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
            line = data + b"\n"
            chunk = f"{len(line):x}\r\n".encode("ascii") + line + b"\r\n"
            _write_cancellable(
                request, run.cancellation, lambda: request.wfile.write(chunk)
            )
            if oversized or event.type == "agent_end" or (compact and event.type == "compaction_end"):
                _write_cancellable(
                    request, run.cancellation, lambda: request.wfile.write(b"0\r\n\r\n")
                )
            if oversized:
                raise RuntimeError("Event exceeds the JSONL byte limit")

        try:
            request.send_response(200)
            request.send_header("Content-Type", "application/jsonl; charset=utf-8")
            request.send_header("Transfer-Encoding", "chunked")
            request.send_header("Connection", "close")
            request.send_header("Cache-Control", "no-store")
            request.send_header("X-Content-Type-Options", "nosniff")
            _write_cancellable(request, run.cancellation, request.end_headers)
            watcher.start()
            if compact:
                session.core.compact(cancellation=run.cancellation, run_id=run.run_id, on_event=emit)
            else:
                session.core.run(message, cancellation=run.cancellation, run_id=run.run_id, on_event=emit)
        except Exception:
            # The core emitted a terminal error, or the client can no longer receive it.
            run.cancellation.cancel()
        finally:
            finished.set()
            if watcher.ident is not None:
                watcher.join()
            with self._lock:
                session.active = None
            run.done.set()

    def close(self) -> None:
        with self._lock:
            already_closing = self._closing
            self._closing = True
        if already_closing:
            self._closed.wait()
            return
        try:
            with self._lock:
                runs = [
                    session.active
                    for session in self._sessions.values()
                    if session.active
                ]
                connections = list(self._connections)
                active_connections = {run.connection for run in runs}
                for session in self._sessions.values():
                    session.deleted = True
            for run in runs:
                run.cancellation.cancel()
            for connection in connections:
                if connection in active_connections:
                    continue
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            for run in runs:
                run.done.wait()
            for connection in active_connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            with self._lock:
                for session in self._sessions.values():
                    session.core.close()
                self._sessions.clear()
        finally:
            self._closed.set()


def serve(
    agent: Agent,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    token: str | None = None,
    allow_remote: bool = False,
    directory: str | None = None,
    budget: ContextBudget | None = None,
) -> AgentServer:
    return AgentServer(
        agent, host=host, port=port, token=token, allow_remote=allow_remote,
        directory=directory, budget=budget,
    )


def _write_cancellable(
    request: _Handler, cancellation: CancellationToken, write: Callable[[], object]
) -> None:
    finished = False
    timer: threading.Timer | None = None
    lock = threading.Lock()

    def disconnect() -> None:
        try:
            request.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def cancelled() -> None:
        nonlocal timer
        with lock:
            if not finished and timer is None:
                timer = threading.Timer(1, disconnect)
                timer.daemon = True
                timer.start()

    remove = cancellation.on_cancel(cancelled)
    try:
        write()
    finally:
        remove()
        with lock:
            finished = True
            if timer:
                timer.cancel()
        if timer:
            timer.join()
