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

PROTOCOL_VERSION = 1
MAX_JSONL_LINE_BYTES = 1_048_576
MAX_HTTP_BODY_BYTES = 1_048_576
MAX_QUEUED_OUTPUT_BYTES = 4_194_304
MAX_TOOL_OUTPUT_BYTES = 131_072

JsonObject = dict[str, Any]
Transport = Callable[[JsonObject], list[JsonObject]]
EventTransport = Callable[[JsonObject, "CancellationToken"], Iterable[JsonObject]]
ToolHandler = Callable[..., Any]
EventHandler = Callable[["AgentEvent"], None]


class CancelledError(RuntimeError):
    """Raised when a run is cooperatively cancelled."""


class ProviderError(RuntimeError):
    """Raised for malformed or failed provider streams."""


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
class Tool:
    name: str
    description: str
    parameters: JsonObject
    handler: ToolHandler
    _validator: Draft202012Validator = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
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

    def execute(self, arguments: JsonObject, cancellation: CancellationToken) -> Any:
        try:
            self._validator.validate(arguments)
        except ValidationError as error:
            path = "/".join(str(part) for part in error.absolute_path)
            raise TypeError(
                f"Invalid arguments for {self.name}/{path}: {error.message}"
            ) from error
        try:
            inspect.signature(self.handler).bind(arguments, cancellation)
        except (TypeError, ValueError):
            return self.handler(arguments)
        return self.handler(arguments, cancellation)


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


@dataclass(frozen=True)
class RunResult:
    text: str
    history: list[JsonObject]
    output: list[JsonObject]


class Provider(Protocol):
    def stream(
        self,
        model: str,
        instructions: str,
        history: list[JsonObject],
        tools: Iterable[Tool],
        cancellation: CancellationToken,
    ) -> Generator[ProviderEvent, None, ProviderTurn]: ...
