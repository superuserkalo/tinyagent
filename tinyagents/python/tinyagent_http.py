"""Threaded HTTP transport for one configured native Agent."""

from __future__ import annotations

import hmac
import ipaddress
import json
import queue
import re
import select
import socket
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Callable, Iterable
from urllib.parse import parse_qs, urlsplit

from tinyagent_http_body import HttpError, read_body
from tinyagent_session import (Session, ContextBudget, NothingToResumeError, RecoveryRequiredError,
                               ResumeRequiredError)
from tinyagent_storage import (FileStorage, SessionBusyError, SessionCorruptionError, SessionFailedError,
                               SessionFencedError, Storage, StorageError)
from tinyagent_types import (
    AgentEvent,
    clean,
    content_problem,
    ended_instead,
    CancellationToken,
    CancelledError,
    JsonObject,
    MAX_JSONL_LINE_BYTES,
    MAX_QUEUED_OUTPUT_BYTES,
    PROTOCOL_VERSION,
)

if TYPE_CHECKING:
    from tiny_agent import Agent


@dataclass
class _Run:
    """One active operation, and the clients following its events."""

    connection: socket.socket
    run_id: str
    followable: bool = True
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    followers: list[_Follower] = field(default_factory=list)
    # Text deltas since the last journal record: the turn in progress, which
    # the journal cannot replay yet.
    buffer: list[tuple[JsonObject, bytes]] = field(default_factory=list)
    buffer_bytes: int = 0
    buffer_cursor: int = -1
    closed: bool = False

    def publish(self, value: JsonObject, line: bytes, cursor: int) -> None:
        with self.lock:
            if value["type"] == "text_delta":
                if cursor != self.buffer_cursor:
                    self.buffer, self.buffer_bytes, self.buffer_cursor = [], 0, cursor
                self.buffer_bytes += len(line)
                if self.buffer_bytes <= MAX_QUEUED_OUTPUT_BYTES:
                    self.buffer.append((value, line))
                else:
                    self.buffer = []
            for follower in self.followers[:]:
                follower.waiting += len(line)
                if follower.waiting > MAX_QUEUED_OUTPUT_BYTES:
                    # A client that does not read is dropped; the run goes on.
                    self.followers.remove(follower)
                    follower.items.put(None)
                else:
                    follower.items.put((value, line))

    def follow(self, cursor: int | None = None) -> tuple[_Follower, list[tuple[JsonObject, bytes]]] | None:
        """Subscribe, and take the buffered deltas if the journal is still at cursor."""
        with self.lock:
            if self.closed:
                return None
            follower = _Follower()
            self.followers.append(follower)
            return follower, list(self.buffer) if cursor == self.buffer_cursor else []

    def close(self, failure: BaseException | None = None) -> None:
        with self.lock:
            self.closed = True
            for follower in self.followers:
                if failure is not None:
                    follower.items.put(failure)
                follower.items.put(None)
            self.followers = []


@dataclass
class _Follower:
    items: queue.SimpleQueue = field(default_factory=queue.SimpleQueue)
    waiting: int = 0


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
            elif isinstance(error, (RecoveryRequiredError, ResumeRequiredError, NothingToResumeError,
                                    SessionFencedError, SessionFailedError)):
                failure = HttpError(409, error.code, str(error))
            elif isinstance(error, FileNotFoundError):
                failure = HttpError(404, "session_not_found", "Unknown session")
            elif isinstance(error, OSError):
                # Nothing has been streamed, so the client is told the one thing it can act on:
                # this session's storage failed and it must be reopened. Never a 500.
                failure = HttpError(409, "session_failed", "Session storage failed; reopen the session")
            elif isinstance(error, SessionCorruptionError):
                failure = HttpError(409, "session_corrupt", str(error))
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
            clean(value), separators=(",", ":"), ensure_ascii=False, allow_nan=False
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
        storage: Storage | None = None,
        budget: ContextBudget | None = None,
        allowed_hosts: Iterable[str] = (),
        allowed_origins: Iterable[str] = (),
    ) -> None:
        """allowed_hosts are extra names accepted in the Host header (the bound address, and
        localhost on loopback, always are). allowed_origins are the only values accepted in an
        Origin header; by default a request that carries one is refused."""
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
        self._application = _Application(agent, token, directory=directory, storage=storage, budget=budget,
                                         bound=address, allowed_hosts=allowed_hosts,
                                         allowed_origins=allowed_origins)
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
                 directory: str | None = None, storage: Storage | None = None,
                 budget: ContextBudget | None = None, bound: Any = None,
                 allowed_hosts: Iterable[str] = (), allowed_origins: Iterable[str] = ()) -> None:
        self._agent = agent
        self._token = token
        self._bound = bound  # the listening address; None when there is no listener to check against
        self._hosts = {name.lower() for name in allowed_hosts}
        self._origins = set(allowed_origins)
        self._storage = storage or (FileStorage(directory) if directory else None)
        self._budget = budget
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        self._connections: set[socket.socket] = set()
        self._closing = False
        self._closed = threading.Event()

    def _host_allowed(self, header: str | None) -> bool:
        """A Host header must name this server: a browser tricked by DNS rebinding sends another name."""
        if self._bound is None:
            return True
        if not header:
            return False
        name = header.strip().lower()
        if name.startswith("["):
            name = name[1:].split("]")[0]
        elif name.rsplit(":", 1)[-1].isdigit():
            name = name.rsplit(":", 1)[0]
        if name in self._hosts or name == str(self._bound):
            return True
        if not self._bound.is_loopback:
            return False
        try:
            return name == "localhost" or ipaddress.ip_address(name).is_loopback
        except ValueError:
            return False

    def _route(self, request: _Handler) -> None:
        # These two checks come first and cover every route, with or without a token.
        if not self._host_allowed(request.headers.get("Host")):
            raise HttpError(421, "misdirected_request", "The Host header does not name this server")
        origin = request.headers.get("Origin")
        if origin is not None and origin not in self._origins:
            raise HttpError(403, "origin_not_allowed", "Requests from this origin are not allowed")
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
                if self._storage:
                    identifiers.update(self._storage.list())
            request._json(200, {"sessionIds": sorted(identifiers)})
            return
        if method == "POST" and path == "/sessions":
            body = read_body(request, required=False) or {}
            identifier = body.get("sessionId")
            if identifier is not None and (not isinstance(identifier, str) or not self._storage):
                raise HttpError(400, "invalid_request", "resume requires storage and a string sessionId")
            with self._lock:
                if self._closing:
                    raise HttpError(503, "server_closing", "Server is shutting down")
                if identifier in self._sessions:
                    raise HttpError(409, "session_busy", "Session is already open")
                core = (Session.open(self._agent, self._storage, identifier, budget=self._budget)
                        if identifier else Session(self._agent, storage=self._storage, budget=self._budget))
                self._sessions[core.id] = _Session(core)
            request._json(201, {"sessionId": core.id})
            return
        match = re.fullmatch(r"/sessions/([^/]+)(?:/(prompts|resume|abandon|events|cancel|compact|reconcile))?", path)
        if not match:
            raise HttpError(404, "not_found", "Unknown route")
        session_id, action = match.groups()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.deleted:
                raise HttpError(404, "session_not_found", "Unknown session")
        query = parse_qs(urlsplit(request.path).query)
        if any(len(values) != 1 for values in query.values()):
            raise HttpError(400, "invalid_request", "Duplicate query options")
        if method == "GET" and action == "events":
            after = int(query.get("after", ["0"])[0])
            if query.get("follow", ["0"])[0] == "1":
                self._follow(request, session, after)
            else:
                request._json(200, session.core.events(after=after, limit=int(query.get("limit", ["100"])[0])))
        elif method == "GET" and action is None:
            request._json(200, session.core.inspect(offset=int(query.get("offset", ["0"])[0]),
                          limit=int(query.get("limit", ["20"])[0]), view=query.get("view", ["history"])[0]))
        elif method == "POST" and action in ("reconcile", "abandon"):
            body = read_body(request, required=action == "reconcile") or {}
            with self._lock:
                if session.active:
                    raise HttpError(409, "session_busy", "Session has an active run")
                if session.deleted or self._closing:
                    raise HttpError(409, "session_closed", "Session is closing")
                if action == "abandon":
                    session.core.abandon(body.get("reason"))
                else:
                    session.core.reconcile(body.get("note"))
            request._json(200, {"abandoned": True} if action == "abandon" else {"recoveryRequired": False})
        elif method == "POST" and action in ("prompts", "compact", "resume"):
            body = read_body(request, required=action == "prompts") or {}
            message = body.get("message")
            if "detach" in body and not isinstance(body["detach"], bool):
                raise HttpError(400, "invalid_request", "detach must be true or false")
            if action == "prompts" and (not message or content_problem(message)):
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
                    # Ask the session what this request will do, so cancel can name the run.
                    plan, stored = ("new", None) if action == "compact" else session.core._plan(
                        body.get("messageId"), action == "resume")
                    if plan == "resume":
                        session.core._answer(**_answer(body))  # refuse bad input before the run starts
                    run = _Run(request.connection, stored or self._agent.new_id("r"),
                               followable=action != "compact")
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
            core, token = session.core, run.cancellation
            if action == "compact":
                start = lambda emit: core.compact(cancellation=token, run_id=run.run_id, on_event=emit)
            elif action == "resume":
                start = lambda emit: core.resume(**_answer(body), cancellation=token, on_event=emit)
            else:
                start = lambda emit: core.run(message, message_id=body.get("messageId"), cancellation=token,
                                              run_id=run.run_id, on_event=emit)
            if body.get("detach") is True and action != "compact":
                self._detached(request, session, run, start)
            else:
                self._prompt(request, session, run, start, compact=action == "compact")
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
        self, request: _Handler, session: _Session, run: _Run,
        start: Callable[[Callable[[AgentEvent], None]], object], *, compact: bool = False
    ) -> None:
        """Run in this request's thread and stream to it. A disconnect cancels the run."""
        finished = threading.Event()
        begun = False
        terminal: tuple[JsonObject, bytes] | None = None
        failure: BaseException | None = None
        cancelled = False
        last = 0

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
            nonlocal begun, terminal, last
            last = event.sequence
            value, line, oversized = _encode(event)
            if oversized:
                run.cancellation.cancel()
            if not begun:
                # Headers wait for the first event, so a refusal is still an HTTP error.
                begun = True
                _write_cancellable(request, run.cancellation, lambda: _stream_headers(request))
            if oversized or event.type == "agent_end" or (compact and event.type == "compaction_end"):
                # The session commits after delivering agent_end to this callback.
                # Hold the terminal line until that commit and the busy-state cleanup
                # have finished: seeing the end must mean the next prompt is accepted.
                terminal = (value, line)
                if oversized:
                    raise RuntimeError("Event exceeds the JSONL byte limit")
                return
            run.publish(value, line, session.core.cursor)
            _write_cancellable(
                request, run.cancellation, lambda: request.wfile.write(_chunk(line))
            )

        try:
            watcher.start()
            start(emit)
        except Exception as error:
            # The core emitted a terminal error, or the client can no longer receive it.
            failure = error
            cancelled = run.cancellation.cancelled
            run.cancellation.cancel()
            if not begun:
                raise
        finally:
            finished.set()
            if watcher.ident is not None:
                watcher.join()
            with self._lock:
                session.active = None
            if begun and not compact:
                terminal = _settled(run, terminal, failure, last, cancelled)
            try:
                if terminal is not None:
                    value, line = terminal
                    run.publish(value, line, session.core.cursor)
                    _write_cancellable(
                        request, run.cancellation, lambda: request.wfile.write(_chunk(line))
                    )
                    _write_cancellable(
                        request, run.cancellation, lambda: request.wfile.write(b"0\r\n\r\n")
                    )
            except Exception:
                # A disconnected client cannot receive the terminal line. The core
                # has already settled and the adapter must still release the run.
                pass
            run.close()
            run.done.set()

    def _detached(self, request: _Handler, session: _Session, run: _Run,
                  start: Callable[[Callable[[AgentEvent], None]], object]) -> None:
        """Run in a thread of its own. This request only follows it, so a disconnect changes nothing."""
        terminal: tuple[JsonObject, bytes] | None = None
        last = 0

        def emit(event: AgentEvent) -> None:
            nonlocal terminal, last
            last = event.sequence
            value, line, oversized = _encode(event)
            if oversized:
                run.cancellation.cancel()
            if oversized or event.type == "agent_end":
                terminal = (value, line)
                if oversized:
                    raise RuntimeError("Event exceeds the JSONL byte limit")
                return
            run.publish(value, line, session.core.cursor)

        def work() -> None:
            failure = None
            cancelled = False
            try:
                start(emit)
            except Exception as error:
                cancelled = run.cancellation.cancelled
                run.cancellation.cancel()
                failure = error
            finally:
                with self._lock:
                    session.active = None
                settled = _settled(run, terminal, failure, last, cancelled) if last else None
                if settled is not None:
                    value, line = settled
                    run.publish(value, line, session.core.cursor)
                run.close(failure)
                run.done.set()

        follower, _ = run.follow()
        threading.Thread(target=work, name=f"tinyagent-{run.run_id}", daemon=True).start()
        first = follower.items.get()
        if isinstance(first, BaseException):
            raise first
        try:
            _stream_headers(request)
            self._relay(request, run, follower, first)
        except (OSError, ValueError):
            pass

    def _follow(self, request: _Handler, session: _Session, after: int) -> None:
        """Replay from the journal, then the turn in progress, then live events to agent_end."""
        core = session.core
        core.events(after=after, limit=1)  # validates before the stream starts
        while True:
            with self._lock:
                run = session.active if session.active and session.active.followable else None
            upto = core.cursor
            joined = run.follow(upto) if run else None
            if run is None or joined is not None:
                break
            # The run closed after it was captured. Retry against the settled
            # journal so a reconnect cannot miss that run's terminal event.
        _stream_headers(request)
        while after is not None:
            page = core.events(after=after, limit=1000)
            for event in page["events"]:
                if event["cursor"] <= upto:
                    request.wfile.write(_chunk(_line(event)))
            after = page["nextAfter"] if not page["events"] or page["events"][-1]["cursor"] <= upto else None
        if joined is None:
            request.wfile.write(b"0\r\n\r\n")
            return
        follower, buffered = joined
        for _, line in buffered:
            request.wfile.write(_chunk(line))
        self._relay(request, run, follower, None, upto)

    def _relay(self, request: _Handler, run: _Run, follower: _Follower,
               item: object, upto: int = 0) -> None:
        """Write a follower's events until the run ends. Events at or before upto were replayed."""
        try:
            while True:
                item = item or follower.items.get()
                if item is None or isinstance(item, BaseException):
                    break
                value, line = item
                with run.lock:
                    follower.waiting -= len(line)
                if value.get("cursor", upto + 1) > upto:
                    request.wfile.write(_chunk(line))
                item = None
            request.wfile.write(b"0\r\n\r\n")
        finally:
            with run.lock:
                if follower in run.followers:
                    run.followers.remove(follower)

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
    storage: Storage | None = None,
    budget: ContextBudget | None = None,
    allowed_hosts: Iterable[str] = (),
    allowed_origins: Iterable[str] = (),
) -> AgentServer:
    return AgentServer(
        agent, host=host, port=port, token=token, allow_remote=allow_remote,
        directory=directory, storage=storage, budget=budget,
        allowed_hosts=allowed_hosts, allowed_origins=allowed_origins,
    )


def _answer(body: JsonObject) -> JsonObject:
    """The result or error a client gave for a paused call. A present key counts, even with null."""
    answer = {"result": body["result"]} if "result" in body else {}
    if body.get("error") is not None:
        answer["error"] = body["error"]
    return answer


def _settled(run: _Run, terminal: tuple[JsonObject, bytes] | None, failure: BaseException | None,
             last: int, cancelled: bool) -> tuple[JsonObject, bytes]:
    """The terminal line a client gets: what the journal records, not only what the loop reported."""
    if terminal is not None and (failure is None or terminal[0].get("status") != "completed"):
        return terminal
    # The commit did not happen, or the loop was closed before it reported an end.
    event = ended_instead(run.run_id, last + (terminal is None), failure if terminal is not None else None,
                          cancelled)
    value = event.as_dict()
    return value, _line(value)


def _line(value: JsonObject) -> bytes:
    # clean(): text that is not valid Unicode is repaired, never a reason to fail a response.
    return json.dumps(clean(value), separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"


def _chunk(line: bytes) -> bytes:
    return f"{len(line):x}\r\n".encode("ascii") + line + b"\r\n"


def _encode(event: AgentEvent) -> tuple[JsonObject, bytes, bool]:
    """An event as one JSONL line; an oversized one becomes a failed agent_end."""
    value = event.as_dict()
    line = _line(value)
    if len(line) - 1 <= MAX_JSONL_LINE_BYTES:
        return value, line, False
    value = {"type": "agent_end", "runId": event.run_id, "sequence": event.sequence, "status": "failed",
             "error": {"code": "output_too_large", "message": "Event exceeds the JSONL byte limit",
                       "retryable": False}}
    return value, _line(value), True


def _stream_headers(request: _Handler) -> None:
    request.send_response(200)
    request.send_header("Content-Type", "application/jsonl; charset=utf-8")
    request.send_header("Transfer-Encoding", "chunked")
    request.send_header("Connection", "close")
    request.send_header("Cache-Control", "no-store")
    request.send_header("X-Content-Type-Options", "nosniff")
    request.end_headers()


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
