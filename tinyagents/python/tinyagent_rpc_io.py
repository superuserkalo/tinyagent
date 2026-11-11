"""Bounded JSONL output and cancellable native pipes with borrowed stream ownership."""

from __future__ import annotations

import errno
import io
import json
import os
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from tinyagent_types import (
    CancellationToken,
    JsonObject,
    MAX_JSONL_LINE_BYTES,
    MAX_QUEUED_OUTPUT_BYTES,
    clean,
)


class RpcOutputLimitError(RuntimeError):
    pass


@dataclass
class _Write:
    line: bytes
    cancellation: CancellationToken | None
    done: threading.Event = field(default_factory=threading.Event)


class RpcConnection:
    def __init__(self, source: Any, output: Any) -> None:
        self.source = source
        self.output = output
        self.cancellation = CancellationToken()
        self.on_output_limit: Callable[[], CancellationToken | None] = lambda: None
        self.failure: Exception | None = None
        self._state_lock = threading.Lock()
        self._writes: queue.Queue[_Write | None] = queue.Queue()
        self._queued_bytes = 0
        self._overflow = False
        self._queue_error = (
            json.dumps(
                {
                    "type": "protocol_error",
                    "error": {
                        "code": "output_queue_full",
                        "message": f"RPC queued output exceeds {MAX_QUEUED_OUTPUT_BYTES} UTF-8 bytes",
                    },
                },
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        self._writer = threading.Thread(
            target=self._write_pipe, name="tinyagent-rpc-output", daemon=True
        )
        self._descriptors: dict[int, bool] = {}
        self._native_input = False
        self._input_buffer = bytearray()
        self._output_fd: int | None = None
        self._flushed = False
        self._interrupted = False
        self._previous_sigint = None
        self._sigint_handler = self._request_interrupt

    def __enter__(self) -> RpcConnection:
        try:
            for stream in (self.source, self.output):
                try:
                    descriptor = stream.fileno()
                except (AttributeError, io.UnsupportedOperation):
                    continue
                if descriptor not in self._descriptors:
                    self._descriptors[descriptor] = os.get_blocking(descriptor)
                    os.set_blocking(descriptor, False)
                if stream is self.output:
                    self._output_fd = descriptor
                if stream is self.source:
                    self._native_input = True
            if (
                threading.current_thread() is threading.main_thread()
                and signal.getsignal(signal.SIGINT) is signal.default_int_handler
            ):
                self._previous_sigint = signal.signal(
                    signal.SIGINT, self._sigint_handler
                )
            self._writer.start()
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, *exception: Any) -> None:
        self.cancellation.cancel()
        if self._writer.ident is not None:
            self._writes.put(None)
            self._writer.join()
        restore_error = None
        for descriptor, blocking in self._descriptors.items():
            try:
                os.set_blocking(descriptor, blocking)
            except OSError as error:
                if error.errno != errno.EBADF:
                    restore_error = error
        if (
            self._previous_sigint is not None
            and signal.getsignal(signal.SIGINT) is self._sigint_handler
        ):
            signal.signal(signal.SIGINT, self._previous_sigint)
        if restore_error:
            raise restore_error
        if exception[0] is None:
            self.raise_if_failed()

    def _request_interrupt(self, signum: int, frame: Any) -> None:
        # Signals can interrupt lock acquisition or Thread.start. Normal I/O
        # polling performs cancellation after the signal handler returns.
        self._interrupted = True

    def readline(self) -> bytes:
        while True:
            self.raise_if_failed()
            if self._interrupted or self._overflow:
                return b""
            if not self._native_input:
                line = self.source.readline(MAX_JSONL_LINE_BYTES + 2)
                break
            newline = self._input_buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._input_buffer[: newline + 1])
                del self._input_buffer[: newline + 1]
                break
            if len(self._input_buffer) > MAX_JSONL_LINE_BYTES:
                line = bytes(self._input_buffer)
                self._input_buffer.clear()
                break
            try:
                chunk = self.source.read(8192)
            except BlockingIOError:
                chunk = None
            if chunk is None:
                time.sleep(0.01)
                continue
            if chunk:
                self._input_buffer.extend(chunk)
                continue
            line = bytes(self._input_buffer)
            self._input_buffer.clear()
            break
        content = line[:-1] if line.endswith(b"\n") else line
        if not line or len(content) > MAX_JSONL_LINE_BYTES:
            self.cancellation.cancel()
        else:
            try:
                content.decode("utf-8")
            except UnicodeDecodeError:
                self.cancellation.cancel()
        return line

    def write(
        self, value: JsonObject, cancellation: CancellationToken | None = None
    ) -> None:
        try:
            # clean(): text that is not valid Unicode is repaired, never a reason to stop.
            data = json.dumps(
                clean(value), separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
        except Exception as error:
            self._fail(error)
            raise
        # Replies queue without blocking command input. Agent events wait for
        # delivery: agent_start holds provider work behind its acknowledgement,
        # and agent_end holds completed history behind its terminal write.
        wait = "sequence" in value
        if len(data) > MAX_JSONL_LINE_BYTES:
            active = self.on_output_limit()
            if cancellation:
                cancellation.cancel()
            error = (
                json.dumps(
                    {
                        "type": "protocol_error",
                        "error": {
                            "code": "output_too_large",
                            "message": f"RPC output exceeds {MAX_JSONL_LINE_BYTES} UTF-8 bytes",
                        },
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            self._enqueue(error, cancellation or active, wait)
            raise RpcOutputLimitError("RPC output line is too large")
        self._enqueue(data + b"\n", cancellation, wait)

    def _enqueue(
        self, line: bytes, cancellation: CancellationToken | None, wait: bool
    ) -> None:
        with self._state_lock:
            if self.failure:
                raise self.failure
            if self._overflow:
                raise RpcOutputLimitError("RPC output queue is full")
            # Reserve one bounded error so overflow itself fits within the limit.
            overflow = self._queued_bytes + len(line) > MAX_QUEUED_OUTPUT_BYTES - len(
                self._queue_error
            )
            if overflow:
                self._overflow = True
                line = self._queue_error
            pending = _Write(line, cancellation)
            self._queued_bytes += len(line)
            self._writes.put(pending)
        if overflow:
            self.cancellation.cancel()
            raise RpcOutputLimitError("RPC output queue is full")
        if wait:
            pending.done.wait()
            self.raise_if_failed()

    def _write_pipe(self) -> None:
        while True:
            pending = self._writes.get()
            if pending is None:
                return
            try:
                self._write_line(pending.line, pending.cancellation)
            except Exception as error:
                self._fail(error)
            finally:
                with self._state_lock:
                    self._queued_bytes -= len(pending.line)
                pending.done.set()

    def _write_line(self, line: bytes, cancellation: CancellationToken | None) -> None:
        deadline: float | None = None

        def cancelled() -> None:
            nonlocal deadline
            if deadline is None:
                deadline = time.monotonic() + 1

        def check() -> None:
            self.raise_if_failed()
            if deadline is not None and time.monotonic() >= deadline:
                raise BrokenPipeError("RPC output did not drain after cancellation")

        remove_stop = self.cancellation.on_cancel(cancelled)
        remove_run = cancellation.on_cancel(cancelled) if cancellation else lambda: None
        try:
            check()
            # Raw descriptor writes leave no buffered stdout data for interpreter
            # shutdown to flush after cancellation. Preserve any caller prefix.
            if not self._flushed:
                while True:
                    check()
                    try:
                        self.output.flush()
                        break
                    except BlockingIOError:
                        time.sleep(0.01)
                self._flushed = True
            offset = 0
            view = memoryview(line)
            while offset < len(line):
                check()
                try:
                    written = (
                        os.write(self._output_fd, view[offset:])
                        if self._output_fd is not None
                        else self.output.write(line[offset:])
                    )
                except BlockingIOError:
                    if self._output_fd is None:
                        raise
                    time.sleep(0.01)
                    continue
                if written is None or written <= 0:
                    raise OSError("RPC output writer made no progress")
                offset += written
            if self._output_fd is None:
                self.output.flush()
        finally:
            remove_run()
            remove_stop()

    def _fail(self, error: Exception) -> None:
        with self._state_lock:
            if self.failure is None:
                self.failure = error
        self.cancellation.cancel()

    def raise_if_failed(self) -> None:
        if self._interrupted:
            self.cancellation.cancel()
        if self.failure is not None:
            raise self.failure
