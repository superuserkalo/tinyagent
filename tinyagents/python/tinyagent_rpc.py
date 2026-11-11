"""JSONL transport around the SDK Session."""

from __future__ import annotations

import inspect
import json
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from tinyagent_types import (
    AgentEvent,
    clean,
    content_problem,
    ended_instead,
    CancellationToken,
    JsonObject,
    RunResult,
    PROTOCOL_VERSION,
    MAX_JSONL_LINE_BYTES,
)
from tinyagent_rpc_io import RpcConnection, RpcOutputLimitError
from tinyagent_session import Session, ContextBudget, NothingToResumeError, ResumeRequiredError
from tinyagent_storage import FileStorage, SessionFailedError, SessionFencedError, Storage, StorageError

if TYPE_CHECKING:
    from tiny_agent import Agent


@dataclass
class _RpcRun:
    run_id: str
    cancellation: CancellationToken
    generation: int
    done: threading.Event = field(default_factory=threading.Event)
    owner: threading.Thread = field(default_factory=threading.current_thread)
    remove_parent: Callable[[], None] = lambda: None
    ended: bool = False


class RpcSession:
    """One RPC connection around a configured Agent and its current Session."""

    def __init__(
        self,
        agent: Agent,
        emit: Callable[..., None],
        diagnostics: Callable[[str], None] | None = None,
        *,
        cancellation: CancellationToken | None = None,
        directory: str | None = None,
        storage: Storage | None = None,
        session_id: str | None = None,
        budget: ContextBudget | None = None,
    ) -> None:
        self.agent = agent
        try:
            inspect.signature(emit).bind({}, None)
        except (TypeError, ValueError):
            self._emit = lambda value, cancellation=None: emit(value)
        else:
            self._emit = lambda value, cancellation=None: emit(value, cancellation)
        self._cancellation = cancellation
        self._diagnostics = diagnostics or (
            lambda message: print(message, file=sys.stderr)
        )
        self._storage = storage or (FileStorage(directory) if directory else None)
        self._budget = budget
        self.session = (Session.open(agent, self._storage, session_id, budget=budget)
                        if self._storage and session_id
                        else Session(agent, storage=self._storage, budget=budget))
        self._active: _RpcRun | None = None
        self._generation = 0
        self._closing = False
        self._lock = threading.Lock()

    def handle(self, request: JsonObject) -> None:
        command = request.get("type")
        if "id" in request and not isinstance(request["id"], str):
            self._emit(
                {
                    "type": "protocol_error",
                    "error": {
                        "code": "invalid_command",
                        "message": "command.id must be a string",
                    },
                }
            )
            return
        request_id = request.get("id")
        if command in ("prompt", "compact", "resume_run"):
            message = request.get("message")
            if command == "prompt" and (not message or content_problem(message)):
                self._command_error(
                    request_id,
                    "prompt",
                    "invalid_command",
                    "prompt.message must be a non-empty string",
                )
                return
            if self._closing:
                self._command_error(request_id, command, "session_closed", "RPC session is closed")
                return
            if command == "compact" and (self.session.resumable or self.session.paused):
                self._command_error(request_id, command, "resume_required",
                                    "The session has an open run; resume or abandon it before compacting")
                return
            if self.session.recovery_required or (command == "compact" and self._budget is None):
                self._command_error(request_id, command, "session_error",
                                    "Reconcile interrupted tools or configure a compaction budget")
                return
            failure = None
            with self._lock:
                if self._active is not None:
                    busy = True
                else:
                    busy = False
                    plan, stored = "new", None
                    if command != "compact":
                        # Ask the session what this command will do, so the reply can name the run.
                        try:
                            plan, stored = self.session._plan(request.get("messageId"), command == "resume_run")
                            if plan == "resume":
                                self.session._answer(**_answer(request))  # refuse bad input before the run starts
                        except (ResumeRequiredError, NothingToResumeError, SessionFailedError) as error:
                            failure = (error.code, str(error))
                        except ValueError as error:
                            failure = ("invalid_command", str(error))
                    if failure is None:
                        run = _RpcRun(
                            run_id=stored or self.agent.new_id("r"),
                            cancellation=CancellationToken(),
                            generation=self._generation,
                        )
                        self._active = run
                        if self._cancellation:
                            run.remove_parent = self._cancellation.on_cancel(
                                run.cancellation.cancel
                            )
            if failure is not None:
                # Emitting may ask for the active cancellation token, which takes
                # this same lock. Never send a command error while holding it.
                self._command_error(request_id, command, *failure)
                return
            if busy:
                self._command_error(
                    request_id, command, "busy", "A run is already active"
                )
                return
            try:
                data: JsonObject = {"runId": run.run_id}
                if command == "prompt":
                    data["repeated"] = plan == "repeat"
                self._emit(_rpc_response(request_id, command, data), run.cancellation)
                worker = threading.Thread(
                    target=self._execute,
                    args=(run, request, command),
                    name=f"tinyagent-{run.run_id}",
                    daemon=True,
                )
                with self._lock:
                    run.owner = worker
                    worker.start()
            except BaseException:
                run.cancellation.cancel()
                self._finish(run)
                raise
        elif command == "abort":
            with self._lock:
                active = self._active
            if active:
                active.cancellation.cancel()
            self._emit(
                _rpc_response(
                    request_id,
                    "abort",
                    {
                        "accepted": active is not None,
                        "runId": active.run_id if active else None,
                    },
                )
            )
        elif self._closing and command in ("new_session", "resume_session"):
            # A closed adapter must not open a journal that nothing will close.
            self._command_error(request_id, command, "session_closed", "RPC session is closed")
        elif command == "new_session":
            with self._lock:
                active = self._active
                self._generation += 1
            if active:
                active.cancellation.cancel()
                if active.owner is not threading.current_thread():
                    active.done.wait()
            self.session.close()
            self.session = Session(self.agent, storage=self._storage, budget=self._budget)
            self._emit(
                _rpc_response(
                    request_id, "new_session", {"abortedRun": active is not None}
                )
            )
        elif command == "get_state":
            with self._lock:
                active = self._active
                message_count = len(self.session.history)
            self._emit(
                _rpc_response(
                    request_id,
                    "get_state",
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "status": "running" if active else "idle",
                        "runId": active.run_id if active else None,
                        "messageCount": message_count,
                        "resumable": self.session.resumable,
                        "paused": self.session.paused,
                        "usage": self.session.usage,
                    },
                )
            )
        elif command in ("list_sessions", "resume_session", "get_history", "get_events", "reconcile",
                         "abandon_run"):
            with self._lock:
                active = self._active
            if active:
                self._command_error(request_id, command, "busy", "A run is already active")
                return
            try:
                if command == "list_sessions":
                    data = {"sessionIds": self._storage.list() if self._storage else [self.session.id]}
                elif command == "resume_session":
                    identifier = request.get("sessionId")
                    if not self._storage or not isinstance(identifier, str):
                        raise ValueError("resume_session requires storage and sessionId")
                    if identifier != self.session.id:
                        resumed = Session.open(self.agent, self._storage, identifier, budget=self._budget)
                        self.session.close()
                        self.session = resumed
                    data = {"sessionId": self.session.id}
                elif command == "get_history":
                    data = self.session.inspect(offset=request.get("offset", 0), limit=request.get("limit", 20),
                                                view=request.get("view", "history"))
                elif command == "get_events":
                    data = self.session.events(after=request.get("after", 0), limit=request.get("limit", 100))
                elif command == "abandon_run":
                    self.session.abandon(request.get("reason"))
                    data = {"abandoned": True}
                elif command == "reconcile":
                    self.session.reconcile(request.get("note"))
                    data = {"sessionId": self.session.id, "recoveryRequired": False}
            except (ValueError, RuntimeError, OSError) as error:
                named = (NothingToResumeError, SessionFailedError, SessionFencedError, StorageError)
                self._command_error(request_id, command,
                                    error.code if isinstance(error, named) else "session_error", str(error))
                return
            self._emit(_rpc_response(request_id, command, data))
        else:
            self._command_error(
                request_id,
                command if isinstance(command, str) else "unknown",
                "invalid_command",
                "Unknown command type",
            )

    def wait_idle(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                active = self._active
            if active is None:
                return True
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            if remaining == 0:
                return False
            if active.owner is threading.current_thread():
                return False
            if not active.done.wait(remaining):
                return False

    def close(self) -> None:
        with self._lock:
            self._closing = True
            active = self._active
        if active:
            active.cancellation.cancel()
            if active.owner is not threading.current_thread():
                active.done.wait()
            else:
                return
        self.session.close()

    def _execute(self, run: _RpcRun, request: JsonObject, command: str) -> None:
        result: RunResult | None = None
        terminal: AgentEvent | None = None
        last = 0

        def emit(event: AgentEvent) -> None:
            nonlocal terminal, last
            last = event.sequence
            # Inside a prompt, compaction events are ordinary; only an explicit compact ends on one.
            if event.type == ("compaction_end" if command == "compact" else "agent_end"):
                # Session commits after its agent_end callback returns. Hold the
                # visible terminal event until the commit and busy-state cleanup
                # are complete, so the client can immediately send its next run.
                terminal = event
                run.ended = run.ended or event.type == "agent_end"
            else:
                self._emit_event(run, event)

        try:
            if command == "compact":
                self.session.compact(cancellation=run.cancellation, run_id=run.run_id, on_event=emit)
            elif command == "resume_run":
                result = self.session.resume(**_answer(request), cancellation=run.cancellation, on_event=emit)
            else:
                result = self.session.run(
                    request["message"], message_id=request.get("messageId"),
                    cancellation=run.cancellation, run_id=run.run_id, on_event=emit,
                )
        except Exception as error:
            if not run.cancellation.cancelled:
                self._diagnostics(f"run {run.run_id} failed: {error}")
            if command != "compact" and (terminal is None or terminal.data.get("status") == "completed"):
                # The journal decides. The loop reported completion but the commit did not happen,
                # or the run stopped without a terminal event: the client is told what is recorded.
                stopped = last > 0 and terminal is None
                terminal = ended_instead(run.run_id, last + (terminal is None),
                                         None if stopped else error, run.cancellation.cancelled)
                run.ended = True
        finally:
            with self._lock:
                if self._active is run:
                    self._active = None
            try:
                if terminal is not None:
                    self._emit_event(run, terminal)
            except Exception:
                # The run is already settled. A client that disappeared while
                # receiving the terminal line cannot change that outcome.
                pass
            finally:
                self._finish(run, result)

    def _finish(self, run: _RpcRun, result: RunResult | None = None) -> None:
        # Completion includes the acknowledgement, worker, and terminal delivery.
        with self._lock:
            if self._active is run:
                self._active = None
            run.remove_parent()
            if self._closing:
                self.session.close()
            run.done.set()

    def _emit_event(self, run: _RpcRun, event: AgentEvent) -> None:
        run.ended = run.ended or event.type == "agent_end"
        try:
            self._emit(event.as_dict(), run.cancellation)
        except Exception:
            run.cancellation.cancel()
            raise

    def _cancel_active(self) -> CancellationToken | None:
        cancellation = self._active_cancellation()
        if cancellation:
            cancellation.cancel()
        return cancellation

    def _active_cancellation(self) -> CancellationToken | None:
        with self._lock:
            active = self._active
            return active.cancellation if active else None

    def _command_error(
        self,
        request_id: str | None,
        command: str,
        code: str,
        message: str,
    ) -> None:
        error = {"code": code, "message": message}
        if request_id is None:
            self._emit({"type": "protocol_error", "error": error})
        else:
            response: JsonObject = {
                "id": request_id,
                "type": "response",
                "command": command,
                "success": False,
                "error": error,
            }
            self._emit(response)


def run_rpc(agent: Agent, input_stream: Any = None, output_stream: Any = None, *,
            directory: str | None = None, storage: Storage | None = None,
            session_id: str | None = None, budget: ContextBudget | None = None) -> None:
    """Run TinyAgent RPC over binary stdin and stdout streams."""

    input_stream = sys.stdin.buffer if input_stream is None else input_stream
    output_stream = sys.stdout.buffer if output_stream is None else output_stream
    with RpcConnection(input_stream, output_stream) as writer:

        def emit(
            value: JsonObject, cancellation: CancellationToken | None = None
        ) -> None:
            writer.write(value, cancellation or session._active_cancellation())

        session = RpcSession(agent, emit, cancellation=writer.cancellation,
                             directory=directory, storage=storage, session_id=session_id, budget=budget)
        writer.on_output_limit = session._cancel_active
        try:
            while True:
                line = writer.readline()
                if line == b"":
                    break
                content = line[:-1] if line.endswith(b"\n") else line
                if len(content) > MAX_JSONL_LINE_BYTES:
                    writer.write(
                        {
                            "type": "protocol_error",
                            "error": {
                                "code": "line_too_large",
                                "message": f"RPC input exceeds {MAX_JSONL_LINE_BYTES} UTF-8 bytes",
                            },
                        }
                    )
                    break
                try:
                    decoded = content.decode("utf-8")
                except UnicodeDecodeError:
                    writer.write(
                        {
                            "type": "protocol_error",
                            "error": {
                                "code": "invalid_utf8",
                                "message": "RPC input is not valid UTF-8",
                            },
                        }
                    )
                    break
                try:
                    request = json.loads(decoded, parse_constant=_reject_json_constant)
                except (ValueError, RecursionError):
                    # Older Pythons raise RecursionError for deeply nested JSON.
                    writer.write(
                        {
                            "type": "protocol_error",
                            "error": {
                                "code": "invalid_json",
                                "message": "RPC input is not valid JSON",
                            },
                        }
                    )
                    continue
                if not isinstance(request, dict):
                    writer.write(
                        {
                            "type": "protocol_error",
                            "error": {
                                "code": "invalid_command",
                                "message": "RPC command must be an object",
                            },
                        }
                    )
                    continue
                try:
                    session.handle(request)
                except RpcOutputLimitError:
                    pass
        except KeyboardInterrupt:
            pass
        finally:
            writer.cancellation.cancel()
            session.close()
            session.session.close()
        writer.raise_if_failed()


def _answer(request: JsonObject) -> JsonObject:
    """The result or error a client gave for a paused call. A present key counts, even with null."""
    answer = {"result": request["result"]} if "result" in request else {}
    if request.get("error") is not None:
        answer["error"] = request["error"]
    return answer


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _rpc_response(request_id: str | None, command: str, data: JsonObject) -> JsonObject:
    response: JsonObject = {
        "type": "response",
        "command": command,
        "success": True,
        "data": data,
    }
    if request_id is not None:
        response["id"] = request_id
    return response
