"""Shared SDK values and run-scoped cancellation."""

from __future__ import annotations

import inspect
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Generator, Iterable, Protocol

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry
from referencing.jsonschema import DRAFT202012

PROTOCOL_VERSION = 2
MAX_JSONL_LINE_BYTES = 1_048_576
MAX_HTTP_BODY_BYTES = 1_048_576
MAX_QUEUED_OUTPUT_BYTES = 4_194_304
MAX_TOOL_OUTPUT_BYTES = 131_072
MAX_SECONDS = 86_400       # the longest timeout or time limit accepted
MAX_DEPTH = 32             # how deeply a prompt, result or option may nest
GRACE_SECONDS = 1.0        # what host code gets to settle after a cancellation or timeout

JsonObject = dict[str, Any]
Transport = Callable[[JsonObject], list[JsonObject]]
EventTransport = Callable[[JsonObject, "CancellationToken"], Iterable[JsonObject]]
ToolHandler = Callable[..., Any]
EventHandler = Callable[["AgentEvent"], None]


def clean(value: Any) -> Any:
    """A copy of a JSON value whose strings are valid Unicode.

    A lone surrogate (half of a character, as a model or a client can send)
    cannot be written as UTF-8, so it becomes U+FFFD.
    """
    if isinstance(value, str):
        try:
            value.encode("utf-8")
            return value
        except UnicodeEncodeError:
            return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    if isinstance(value, dict):
        return {clean(key): clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    return value


def too_deep(value: Any, limit: int = MAX_DEPTH) -> bool:
    """True when a JSON value nests more than limit levels."""
    if not isinstance(value, (dict, list, tuple)):
        return False
    if limit == 0:
        return True
    return any(too_deep(item, limit - 1) for item in (value.values() if isinstance(value, dict) else value))


def check_seconds(value: Any, name: str) -> None:
    """A timeout or time limit: None, or a finite positive number of at most MAX_SECONDS."""
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                              or not 0 < value <= MAX_SECONDS):
        raise ValueError(f"{name} must be a positive number of seconds, at most {MAX_SECONDS}")


def check_run_options(cancellation: Any, options: JsonObject) -> None:
    """Refuse bad run options before a run starts, so nothing is left half-begun."""
    if cancellation is not None and not isinstance(cancellation, CancellationToken):
        raise ValueError("cancellation must be a CancellationToken")
    known = {"context", "instructions", "model", "extra", "output_schema", "trace",
             "max_steps", "max_seconds", "max_tokens"}
    if set(options) - known:
        raise ValueError(f"unknown run option: {sorted(set(options) - known)[0]}")
    for name, kind in (("instructions", str), ("model", str), ("extra", dict), ("output_schema", dict),
                       ("trace", bool)):
        if options.get(name) is not None and not isinstance(options[name], kind):
            raise ValueError(f"{name} must be a {kind.__name__}")
    for name in ("max_steps", "max_tokens"):
        value = options.get(name)
        if value is not None and (type(value) is not int or not 0 < value < 2 ** 53):
            raise ValueError(f"{name} must be a positive integer")
    check_seconds(options.get("max_seconds"), "max_seconds")
    if options.get("output_schema") is not None and options["output_schema"].get("type") != "object":
        raise ValueError("output_schema must describe a JSON object")
    if too_deep(options.get("extra")) or too_deep(options.get("output_schema")):
        raise ValueError(f"run options nest deeper than {MAX_DEPTH} levels")


def content_problem(content: Any) -> str | None:
    """Why this is not a prompt or tool output: a string, or a list of text and image parts."""
    if isinstance(content, str):
        return None
    if too_deep(content):
        return f"nests deeper than {MAX_DEPTH} levels"
    if not isinstance(content, list) or not content:
        return "must be a string or a non-empty list of parts"
    for part in content:
        if not isinstance(part, dict):
            return "parts must be objects"
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            continue
        if part.get("type") == "image" and (isinstance(part.get("url"), str) or (
                isinstance(part.get("mediaType"), str) and isinstance(part.get("data"), str))):
            continue
        return "a part must be {type: text, text} or {type: image, url} or {type: image, mediaType, data}"
    return None


class CancelledError(RuntimeError):
    """Raised when a run is cooperatively cancelled."""


class ProviderError(RuntimeError):
    """Raised for malformed or failed provider streams.

    retryable marks a failure before any of the response body arrived that is
    worth trying again (HTTP 429, 500, 502, 503, 504, or a connection error).
    """

    def __init__(self, message: str = "", *, status: int | None = None,
                 retry_after: float | None = None, retryable: bool = False) -> None:
        # A provider's error text is kept short wherever it travels: events, logs, the journal.
        super().__init__(str(message)[:500])
        self.status = status
        self.retry_after = retry_after
        self.retryable = retryable


class ToolError(RuntimeError):
    """Raise from a tool handler to give the model this exact text as a tool error."""


class ToolDenied(ToolError):
    """before_tool refused the call. The reason is the tool error; after_tool does not run."""


class LimitExceededError(RuntimeError):
    """The run reached max_steps, max_seconds or max_tokens."""

    code = "limit_exceeded"

    def __init__(self, limit: str) -> None:
        super().__init__(f"run limit exceeded: {limit}")
        self.limit = limit


class HookError(RuntimeError):
    """before_tool or after_tool raised, or returned something unusable."""

    code = "hook_error"


class OutputMissingError(RuntimeError):
    """The model answered twice without calling the output tool."""

    code = "output_missing"


class RepeatedToolErrorsError(RuntimeError):
    """Every tool call failed in three consecutive model turns."""

    code = "repeated_tool_errors"


class ToolOutputLimitError(RuntimeError):
    """Raised when a tool observation exceeds the configured core limit."""


class CancellationToken:
    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        with self._lock:
            if self._cancelled.is_set():
                return
            self._cancelled.set()
            callbacks = list(self._callbacks)
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise CancelledError("run aborted")

    def wait(self, seconds: float) -> bool:
        """Sleep for up to seconds, returning early (True) when cancelled."""
        return self._cancelled.wait(seconds)

    def on_cancel(self, callback: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            if not self.cancelled:
                self._callbacks.append(callback)
                registered = True
            else:
                registered = False
        if not registered:
            try:
                callback()
            except Exception:
                pass

        def remove() -> None:
            with self._lock:
                if callback in self._callbacks:
                    self._callbacks.remove(callback)

        return remove


@dataclass(frozen=True)
class Pause:
    """Returned by a tool handler or before_tool to end the run as paused."""

    payload: Any = None


def pause(payload: Any = None) -> Pause:
    """Pause the run at this tool call; session.resume(result=...) continues it."""
    return Pause(payload)


class ToolContext:
    """What a tool handler receives as its second argument.

    cancellation is the call's signal, execution_id is stable across a rerun,
    and context is the host value passed to the run. For handlers written
    against version 1, it also answers the CancellationToken methods.
    """

    def __init__(self, cancellation: CancellationToken, execution_id: str,
                 context: Any = None) -> None:
        self.cancellation = cancellation
        self.execution_id = execution_id
        self.context = context
        self.timed_out = False

    @property
    def cancelled(self) -> bool:
        return self.cancellation.cancelled

    def raise_if_cancelled(self) -> None:
        self.cancellation.raise_if_cancelled()

    def on_cancel(self, callback: Callable[[], None]) -> Callable[[], None]:
        return self.cancellation.on_cancel(callback)

    def cancel(self) -> None:
        self.cancellation.cancel()

    def wait(self, seconds: float) -> bool:
        return self.cancellation.wait(seconds)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: JsonObject
    handler: ToolHandler
    idempotent: bool = False
    timeout_seconds: float | None = None
    _validator: Draft202012Validator = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        check_seconds(self.timeout_seconds, "timeout_seconds")
        parameters = deepcopy(self.parameters)
        if parameters.get("$async"):
            raise ValueError("Tool schemas must be synchronous")
        Draft202012Validator.check_schema(parameters)
        registry = Registry()
        resource = DRAFT202012.create_resource(parameters)
        pending = [(resource, registry.resolver_with_root(resource))]
        while pending:
            schema, resolver = pending.pop()
            if isinstance(schema.contents, dict):
                for keyword in ("$ref", "$dynamicRef"):
                    if keyword in schema.contents:
                        resolver.lookup(schema.contents[keyword])
            pending.extend(
                (child, resolver.in_subresource(child))
                for child in schema.subresources()
            )
        object.__setattr__(self, "parameters", parameters)
        object.__setattr__(
            self, "_validator", Draft202012Validator(parameters, registry=registry)
        )

    def definition(self) -> JsonObject:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "strict": True,
            "parameters": self.parameters,
        }

    def validate(self, arguments: JsonObject) -> None:
        try:
            self._validator.validate(arguments)
        except ValidationError as error:
            path = "/".join(str(part) for part in error.absolute_path)
            raise TypeError(
                f"Invalid arguments for {self.name}/{path}: {error.message}"
            ) from error

    def execute(self, arguments: JsonObject, context: ToolContext | CancellationToken) -> Any:
        """Validate, then call handler(arguments, context) or handler(arguments)."""
        self.validate(arguments)
        try:
            inspect.signature(self.handler).bind(arguments, context)
        except (TypeError, ValueError):
            return self.handler(arguments)
        return self.handler(arguments, context)


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: JsonObject


@dataclass(frozen=True)
class ProviderEvent:
    type: str
    delta: str


@dataclass(frozen=True)
class ProviderTurn:
    text: str
    assistant: JsonObject
    output: list[JsonObject]
    tool_calls: list[ToolCall]
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class AgentEvent:
    type: str
    run_id: str
    sequence: int
    data: JsonObject

    def as_dict(self) -> JsonObject:
        return {
            "type": self.type,
            "runId": self.run_id,
            "sequence": self.sequence,
            **self.data,
        }


def ended_instead(run_id: str, sequence: int, error: BaseException | None, cancelled: bool,
                  usage: JsonObject | None = None) -> AgentEvent:
    """The agent_end an adapter delivers when a run did not end the way its loop reported.

    The journal decides the outcome: a run whose commit failed is failed, a
    run cancelled before its commit or closed without a terminal event is aborted.
    """
    usage = usage or {"inputTokens": 0, "outputTokens": 0, "requests": 0}  # every agent_end carries usage
    if cancelled or error is None:  # only the run's own cancellation makes it aborted
        return AgentEvent("agent_end", run_id, sequence, {"status": "aborted", "usage": usage})
    return AgentEvent("agent_end", run_id, sequence, {"status": "failed", "usage": usage, "error": {
        "code": getattr(error, "code", "session_error"), "message": str(error)[:500], "retryable": False}})


@dataclass(frozen=True)
class RunResult:
    text: str
    history: list[JsonObject]
    output: Any
    usage: JsonObject = field(default_factory=lambda: {"inputTokens": 0, "outputTokens": 0, "requests": 0})
    value: Any = None  # the validated final_output arguments of a run with output_schema
    repeated: bool = False
    status: str = "completed"
    pause: JsonObject | None = None


class Provider(Protocol):
    def stream(
        self,
        model: str,
        instructions: str,
        history: list[JsonObject],
        tools: Iterable[Tool],
        cancellation: CancellationToken,
    ) -> Generator[ProviderEvent, None, ProviderTurn]: ...
