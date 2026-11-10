"""Binary JSONL framing through the public RPC driver."""

import io
import json
import os
import signal
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tinyagent import (
    Agent,
    MAX_JSONL_LINE_BYTES,
    MAX_QUEUED_OUTPUT_BYTES,
    ProviderEvent,
    run_rpc,
)


class ShortOutput(io.BytesIO):
    def write(self, data):
        return super().write(data[:3])


class RpcFramingTests(unittest.TestCase):
    def test_short_writes_preserve_complete_utf8_json_lines(self):
        commands = [
            {"id": "🔥\n雪", "type": "get_state"},
            {"id": "abort", "type": "abort"},
            {"id": "reset", "type": "new_session"},
        ]
        source = io.BytesIO(
            b"\n".join(json.dumps(command).encode() for command in commands)
        )
        target = ShortOutput()
        run_rpc(Agent(model="test", transport=lambda payload: []), source, target)
        output = [json.loads(line) for line in target.getvalue().splitlines()]
        self.assertEqual(
            [item["id"] for item in output], [item["id"] for item in commands]
        )
        self.assertEqual(
            [item["command"] for item in output], [item["type"] for item in commands]
        )

    def test_writer_without_progress_fails_instead_of_acknowledging_output(self):
        for result in (0, None):
            with self.subTest(result=result):

                class StalledOutput(io.BytesIO):
                    def write(self, data):
                        return result

                with self.assertRaises(OSError):
                    run_rpc(
                        Agent(model="test", transport=lambda payload: []),
                        io.BytesIO(b'{"type":"get_state"}\n'),
                        StalledOutput(),
                    )

    def test_non_json_numbers_are_rejected_without_closing_the_connection(self):
        source = io.BytesIO(
            b"\n".join(
                b'{"type":"get_state","extra":' + number + b"}"
                for number in (b"NaN", b"Infinity", b"-Infinity")
            )
            + b'\n{"id":"NaN","type":"get_state"}\n'
        )
        target = io.BytesIO()
        run_rpc(Agent(model="test", transport=lambda payload: []), source, target)
        output = [json.loads(line) for line in target.getvalue().splitlines()]
        self.assertEqual(
            [item.get("error", {}).get("code") for item in output],
            ["invalid_json"] * 3 + [None],
        )
        self.assertEqual(output[-1]["id"], "NaN")
        self.assertEqual(output[-1]["data"]["status"], "idle")

    def test_deeply_nested_input_is_rejected_without_closing_the_connection(self):
        source = io.BytesIO(
            b"[" * 10000 + b"0" + b"]" * 10000 + b'\n{"id":"next","type":"get_state"}\n'
        )
        target = io.BytesIO()
        run_rpc(Agent(model="test", transport=lambda payload: []), source, target)
        output = [json.loads(line) for line in target.getvalue().splitlines()]
        self.assertEqual(len(output), 2)
        self.assertEqual(output[0]["type"], "protocol_error")
        # Pythons that can parse this depth see a non-object command instead.
        self.assertIn(output[0]["error"]["code"], ("invalid_json", "invalid_command"))
        self.assertEqual(output[1]["id"], "next")

    def test_oversized_command_reply_is_bounded_and_allows_the_next_command(self):
        source = io.BytesIO(
            json.dumps(
                {"type": "get_state", "id": "x" * (MAX_JSONL_LINE_BYTES - 64)}
            ).encode()
            + b'\n{"id":"next","type":"get_state"}\n'
        )
        target = ShortOutput()
        run_rpc(Agent(model="test", transport=lambda payload: []), source, target)
        self.assertLess(len(target.getvalue()), 500)
        output = [json.loads(line) for line in target.getvalue().splitlines()]
        self.assertEqual(output[0]["error"]["code"], "output_too_large")
        self.assertEqual(output[1]["id"], "next")

    def test_native_streams_preserve_buffered_input_and_descriptor_ownership(self):
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "rb") as source, os.fdopen(
            write_fd, "wb"
        ) as sender, tempfile.TemporaryFile() as target:
            sender.write(b'prefix\n{"id":"state","type":"get_state"}\n')
            sender.close()
            self.assertEqual(source.readline(), b"prefix\n")
            target.write(b"prefix\n")
            os.set_blocking(target.fileno(), False)
            run_rpc(Agent(model="test", transport=lambda payload: []), source, target)
            self.assertTrue(os.get_blocking(source.fileno()))
            self.assertFalse(os.get_blocking(target.fileno()))
            self.assertFalse(source.closed)
            self.assertFalse(target.closed)
            target.seek(0)
            self.assertEqual(target.readline(), b"prefix\n")
            self.assertEqual(json.loads(target.readline())["id"], "state")

    def test_failed_output_releases_native_reader_and_provider_before_returning(self):
        closed = []

        class Provider:
            def stream(self, model, instructions, history, tools, token):
                try:
                    yield ProviderEvent("text_delta", "fail")
                finally:
                    closed.append(token.cancelled)

        class FailedOutput(io.BytesIO):
            def write(self, data):
                if b'"text_delta"' in data:
                    raise OSError("consumer failed")
                return super().write(data)

        read_fd, write_fd = os.pipe()
        before = {thread.ident for thread in threading.enumerate()}
        with os.fdopen(read_fd, "rb") as source, os.fdopen(write_fd, "wb") as sender:
            sender.write(b'{"type":"prompt","message":"go"}\n')
            sender.flush()
            with self.assertRaisesRegex(OSError, "consumer failed"):
                run_rpc(
                    Agent(model="test", provider=Provider()), source, FailedOutput()
                )
            self.assertTrue(os.get_blocking(source.fileno()))
            self.assertFalse(source.closed)
        self.assertEqual(closed, [True])
        self.assertFalse(
            [
                thread
                for thread in threading.enumerate()
                if thread.ident not in before
                and thread.name.startswith("tinyagent-rpc-")
            ]
        )

    def test_signal_handler_is_restored_and_custom_handlers_remain_owned_by_caller(
        self,
    ):
        previous = signal.getsignal(signal.SIGINT)
        try:
            for handler in (signal.default_int_handler, lambda signum, frame: None):
                with self.subTest(custom=handler is not signal.default_int_handler):
                    signal.signal(signal.SIGINT, handler)

                    class Output(io.BytesIO):
                        def write(self, data):
                            if handler is not signal.default_int_handler:
                                self_check.assertIs(
                                    signal.getsignal(signal.SIGINT), handler
                                )
                            raise OSError("write failed")

                    self_check = self
                    with self.assertRaisesRegex(OSError, "write failed"):
                        run_rpc(
                            Agent(model="test", transport=lambda payload: []),
                            io.BytesIO(b'{"type":"get_state"}\n'),
                            Output(),
                        )
                    self.assertIs(signal.getsignal(signal.SIGINT), handler)
        finally:
            signal.signal(signal.SIGINT, previous)

    @unittest.skipUnless(
        os.name == "posix", "The startup interruption check sends SIGINT"
    )
    def test_interrupt_during_worker_start_waits_for_provider_cleanup(self):
        started = threading.Event()
        closed = threading.Event()
        start_thread = threading.Thread.start

        class Provider:
            def stream(self, model, instructions, history, tools, token):
                stopped = threading.Event()
                remove = token.on_cancel(stopped.set)
                started.set()
                try:
                    stopped.wait(2)
                    token.raise_if_cancelled()
                    yield from ()
                finally:
                    time.sleep(0.05)
                    remove()
                    closed.set()

        def start(thread):
            start_thread(thread)
            if thread.name.startswith("tinyagent-r_"):
                self.assertTrue(started.wait(1))
                os.kill(os.getpid(), signal.SIGINT)

        output = io.BytesIO()
        with patch.object(threading.Thread, "start", start):
            run_rpc(
                Agent(model="test", provider=Provider()),
                io.BytesIO(b'{"type":"prompt","message":"go"}\n'),
                output,
            )
        self.assertTrue(closed.is_set())
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(events[-1]["status"], "aborted")

    def test_output_queue_overflow_cancels_and_emits_one_bounded_error(self):
        started = threading.Event()
        closed = threading.Event()
        blocked = threading.Event()
        release = threading.Event()
        errors = []
        command = (
            json.dumps(
                {"type": "get_state", "id": "x" * (MAX_JSONL_LINE_BYTES // 2)}
            ).encode()
            + b"\n"
        )

        class Provider:
            def stream(self, model, instructions, history, tools, token):
                stop = threading.Event()
                remove = token.on_cancel(stop.set)
                started.set()
                try:
                    stop.wait(3)
                    token.raise_if_cancelled()
                    yield from ()
                finally:
                    remove()
                    closed.set()

        class Source:
            reads = 0

            def readline(self, limit):
                self.reads += 1
                if self.reads == 1:
                    return b'{"type":"prompt","message":"go"}\n'
                if not started.wait(2):
                    raise AssertionError("provider did not start")
                return command if self.reads < 20 else b""

        class Output(io.BytesIO):
            def write(self, data):
                if len(data) > 100_000:
                    blocked.set()
                    if not release.wait(3):
                        raise AssertionError("output was not released")
                return super().write(data)

        source, output = Source(), Output()

        def run():
            try:
                run_rpc(Agent(model="test", provider=Provider()), source, output)
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            self.assertTrue(blocked.wait(2))
            self.assertTrue(closed.wait(2))
            self.assertLess(source.reads, 20)
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        lines = output.getvalue().splitlines(keepends=True)
        values = [json.loads(line) for line in lines]
        queue_errors = [
            value
            for value in values
            if value.get("error", {}).get("code") == "output_queue_full"
        ]
        self.assertEqual(len(queue_errors), 1)
        pending_bytes = sum(
            len(line)
            for line, value in zip(lines, values)
            if value.get("command") == "get_state" or value["type"] == "protocol_error"
        )
        self.assertLessEqual(pending_bytes, MAX_QUEUED_OUTPUT_BYTES)


if __name__ == "__main__":
    unittest.main()
