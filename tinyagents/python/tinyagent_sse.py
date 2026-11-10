"""Bounded provider SSE framing and cancellable native HTTP requests."""

from __future__ import annotations

import http.client
import json
import math
import socket
import threading
import time
from typing import Iterator, Protocol
from urllib.parse import urlsplit

from tinyagent_types import CancellationToken, JsonObject, ProviderError

MAX_SSE_BYTES = 1_048_576


class Reader(Protocol):
    def read1(self, size: int) -> bytes: ...


class Arguments:
    """Count UTF-8 once per delta instead of repeatedly copying the whole buffer."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._bytes = 0

    def append(self, value: str) -> None:
        added = len(value.encode("utf-8"))
        if added > MAX_SSE_BYTES - self._bytes:
            raise ProviderError("tool arguments exceed their byte limit")
        self._bytes += added
        self._parts.append(value)

    def set(self, value: str) -> None:
        self._parts.clear()
        self._bytes = 0
        self.append(value)

    def value(self) -> str:
        return "".join(self._parts)


def object_value(value: object, name: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ProviderError(f"invalid provider object: {name}")
    return value


def string_value(value: JsonObject, key: str, *, required: bool = False) -> str:
    result = value.get(key)
    if not isinstance(result, str) or (required and not result):
        raise ProviderError(f"invalid provider string: {key}")
    return result


def index_value(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderError("invalid provider block index")
    return value


def parse_object(source: str) -> JsonObject:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-JSON number: {value}")

    try:
        return object_value(json.loads(source, parse_constant=reject_constant), "JSON")
    except (ValueError, RecursionError) as error:
        raise ProviderError(f"invalid provider JSON: {error}") from error


def sse_events(source: Reader, cancellation: CancellationToken) -> Iterator[JsonObject]:
    pending = bytearray()
    data: list[str] = []
    data_bytes = 0
    scan_from = 0
    while True:
        cancellation.raise_if_cancelled()
        chunk = source.read1(8192)
        pending.extend(chunk)
        while True:
            end = pending.find(b"\n", scan_from)
            if end < 0:
                scan_from = len(pending)
                if chunk or not pending:
                    break
            length = end if end >= 0 else len(pending)
            if length > MAX_SSE_BYTES:
                raise ProviderError("SSE line exceeds its byte limit")
            raw = bytes(pending[:length])
            del pending[: length + (end >= 0)]
            scan_from = 0
            try:
                line = raw.removesuffix(b"\r").decode("utf-8")
            except UnicodeDecodeError as error:
                raise ProviderError("provider stream is not valid UTF-8") from error
            if line == "":
                if data:
                    value = "\n".join(data)
                    if value == "[DONE]":
                        raise ProviderError(
                            "provider stream ended without a complete turn"
                        )
                    yield parse_object(value)
                data = []
                data_bytes = 0
            elif line == "data" or line.startswith("data:"):
                value = "" if line == "data" else line[5:]
                if value.startswith(" "):
                    value = value[1:]
                data_bytes += len(value.encode("utf-8")) + bool(data)
                if data_bytes > MAX_SSE_BYTES:
                    raise ProviderError("SSE event exceeds its byte limit")
                data.append(value)
            cancellation.raise_if_cancelled()
        if len(pending) > MAX_SSE_BYTES:
            raise ProviderError("SSE line exceeds its byte limit")
        if not chunk:
            if data:
                value = "\n".join(data)
                if value == "[DONE]":
                    raise ProviderError("provider stream ended without a complete turn")
                yield parse_object(value)
            return


def http_sse_events(
    url: str,
    payload: JsonObject,
    headers: dict[str, str],
    cancellation: CancellationToken,
    timeout: float = 180,
) -> Iterator[JsonObject]:
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("provider timeout must be positive and finite")
    address = urlsplit(url)
    if (
        address.scheme not in ("http", "https")
        or not address.hostname
        or address.username
        or address.fragment
    ):
        raise ValueError("provider endpoint must be an HTTP or HTTPS URL")
    cancellation.raise_if_cancelled()
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    connection_type = (
        http.client.HTTPSConnection
        if address.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_type(address.hostname, address.port, timeout=timeout)
    token = CancellationToken()
    expired = threading.Event()
    active_socket: socket.socket | None = None
    response: http.client.HTTPResponse | None = None
    deadline = time.monotonic() + timeout

    def stop_read() -> None:
        # HTTPResponse.close() can wait for a reader's lock. Shutdown releases
        # that reader first, including when getresponse detached the socket.
        if active_socket is not None:
            try:
                active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def expire() -> None:
        expired.set()
        token.cancel()

    remove_stop = token.on_cancel(stop_read)
    remove_parent = cancellation.on_cancel(token.cancel)
    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    try:
        token.raise_if_cancelled()
        connection.connect()
        active_socket = connection.sock
        token.raise_if_cancelled()
        if active_socket is not None:
            active_socket.settimeout(max(0.001, deadline - time.monotonic()))
        target = address.path or "/"
        if address.query:
            target += "?" + address.query
        connection.request("POST", target, body=body, headers=headers)
        response = connection.getresponse()
        token.raise_if_cancelled()
        if not 200 <= response.status < 300:
            detail = response.read(8192).decode("utf-8", errors="replace")
            raise ProviderError(f"API error {response.status}: {detail}")
        yield from sse_events(response, token)
        token.raise_if_cancelled()
    except Exception as error:
        if expired.is_set():
            raise ProviderError("provider request timed out") from error
        token.raise_if_cancelled()
        raise
    finally:
        timer.cancel()
        timer.join()
        remove_parent()
        remove_stop()
        stop_read()
        if response is not None:
            response.close()
        connection.close()
