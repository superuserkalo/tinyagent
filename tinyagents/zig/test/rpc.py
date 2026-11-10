#!/usr/bin/env python3
"""Exercise the native Zig RPC executable through real subprocess pipes."""

import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
import unittest


EXECUTABLE = str(Path(sys.argv.pop(1)).resolve())
LIMIT = 1_048_576


class Process:
    def __init__(self, cleanup_fd=None):
        arguments = [EXECUTABLE]
        if cleanup_fd is not None:
            arguments.append(str(cleanup_fd))
        self.child = subprocess.Popen(
            arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0,
            pass_fds=() if cleanup_fd is None else (cleanup_fd,),
        )
        self.buffer = bytearray()
        self.diagnostics = bytearray()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.child.poll() is None:
            self.child.kill()
        self.child.communicate(timeout=5)

    def send(self, value):
        self.write(json.dumps(value, ensure_ascii=False).encode() + b"\n")

    def write(self, data):
        while data:
            count = os.write(self.child.stdin.fileno(), data)
            data = data[count:]

    def eof(self):
        self.child.stdin.close()
        self.child.stdin = None

    def read(self, count=65536, timeout=5):
        ready, _, _ = select.select([self.child.stdout], [], [], timeout)
        if not ready:
            raise AssertionError("RPC output timed out")
        data = os.read(self.child.stdout.fileno(), count)
        if not data:
            raise AssertionError(f"RPC output ended: {self.child.poll()}")
        self.buffer.extend(data)

    def line(self):
        deadline = time.monotonic() + 5
        while b"\n" not in self.buffer:
            self.read(timeout=max(0, deadline - time.monotonic()))
        line, _, remaining = self.buffer.partition(b"\n")
        self.buffer = bytearray(remaining)
        if len(line) > LIMIT:
            raise AssertionError("RPC output exceeded its line limit")
        return json.loads(line)

    def cancelled_wait(self):
        deadline = time.monotonic() + 5
        while b"cancelled wait\n" not in self.diagnostics:
            ready, _, _ = select.select([self.child.stderr], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                raise AssertionError("Native cancellation did not reach the pending write receipt")
            data = os.read(self.child.stderr.fileno(), 4096)
            if not data:
                raise AssertionError("Process exited before cancellation")
            self.diagnostics.extend(data)

    def until(self, predicate):
        values = []
        for _ in range(100):
            value = self.line()
            values.append(value)
            if predicate(value):
                return values
        raise AssertionError("Expected RPC response was not received")

    def prompt(self, message):
        self.send({"id": "prompt", "type": "prompt", "message": message})
        return self.until(lambda item: item["type"] == "agent_end")

    def idle(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.send({"id": "state", "type": "get_state"})
            state = self.until(lambda item: item.get("id") == "state")[-1]["data"]
            if state["status"] == "idle":
                return state
            time.sleep(0.005)
        raise AssertionError("Session did not finish cleanup")

    def finish(self, success=True):
        if self.child.stdin is not None:
            self.eof()
        output, diagnostics = self.child.communicate(timeout=5)
        diagnostics = bytes(self.diagnostics) + diagnostics
        output = bytes(self.buffer) + output
        self.buffer.clear()
        if (self.child.returncode == 0) != success:
            raise AssertionError(diagnostics.decode(errors="replace"))
        if b"descriptors preserved" not in diagnostics:
            raise AssertionError("RPC closed or changed borrowed descriptors")
        if b"error(gpa)" in diagnostics:
            raise AssertionError(diagnostics.decode(errors="replace"))
        return output, diagnostics


class RpcTests(unittest.TestCase):
    def test_shared_fixed_responses(self):
        fixture = json.loads(Path(__file__).with_name("fixtures").joinpath("rpc_v1.json").read_text())
        with Process() as process:
            for scenario in fixture["scenarios"]:
                if "expected" not in scenario:
                    continue
                for command in scenario["commands"]:
                    process.send(command)
                actual = [process.line() for _ in scenario["expected"]]
                self.assertEqual(scenario["expected"], actual, scenario["name"])
            process.finish()

    def test_completion_order_and_jsonl_unicode(self):
        with Process() as process:
            values = process.prompt('café\n"quoted" 🙂')
            self.assertEqual(["response", "agent_start", "text_delta", "agent_end"], [v["type"] for v in values])
            run_id = values[0]["data"]["runId"]
            self.assertRegex(run_id, r"^r_[0-9a-f]{32}$")
            self.assertEqual([1, 2, 3], [v["sequence"] for v in values[1:]])
            self.assertTrue(all(v["runId"] == run_id for v in values[1:]))
            self.assertEqual('café\n"quoted" 🙂', values[-1]["text"])
            self.assertEqual(2, process.idle()["messageCount"])
            process.finish()

    def test_commit_failure_rollback_and_reset(self):
        with Process() as process:
            self.assertEqual("first", process.prompt("first")[-1]["text"])
            self.assertEqual(2, process.idle()["messageCount"])
            failure = process.prompt("fail")[-1]
            self.assertEqual("failed", failure["status"])
            self.assertEqual("provider_error", failure["error"]["code"])
            self.assertEqual(2, process.idle()["messageCount"])
            self.assertEqual("3", process.prompt("history")[-1]["text"])
            self.assertEqual(4, process.idle()["messageCount"])
            process.send({"id": "new", "type": "new_session"})
            self.assertEqual({"abortedRun": False}, process.line()["data"])
            self.assertEqual(0, process.idle()["messageCount"])
            process.finish()

    def test_busy_abort_and_rollback(self):
        with Process() as process:
            process.send({"type": "prompt", "message": "wait"})
            first = process.until(lambda item: item["type"] == "text_delta")
            run_id = first[0]["data"]["runId"]
            process.send({"id": "busy", "type": "prompt", "message": "second"})
            process.send({"id": "abort", "type": "abort"})
            values = [process.line() for _ in range(3)]
            responses = {v.get("id"): v for v in values}
            self.assertEqual("busy", responses["busy"]["error"]["code"])
            self.assertEqual({"accepted": True, "runId": run_id}, responses["abort"]["data"])
            self.assertEqual("aborted", next(v for v in values if v["type"] == "agent_end")["status"])
            self.assertEqual(0, process.idle()["messageCount"])
            process.finish()

    def test_active_reset_finishes_before_reply(self):
        with Process() as process:
            process.send({"type": "prompt", "message": "wait"})
            process.until(lambda item: item["type"] == "text_delta")
            process.send({"id": "new", "type": "new_session"})
            self.assertEqual("aborted", process.line()["status"])
            self.assertEqual({"abortedRun": True}, process.line()["data"])
            self.assertEqual("1", process.prompt("history")[-1]["text"])
            process.idle()
            process.finish()

    def test_eof_and_interrupt_cancel_active_provider(self):
        for interrupt in (False, True):
            with self.subTest(interrupt=interrupt), Process() as process:
                process.send({"type": "prompt", "message": "wait"})
                process.until(lambda item: item["type"] == "text_delta")
                if interrupt:
                    process.child.send_signal(signal.SIGINT)
                else:
                    process.eof()
                self.assertEqual("aborted", process.line()["status"])
                output, diagnostics = process.finish()
                self.assertEqual(b"", output)
                self.assertIn(b"cleanup", diagnostics)

    def test_cancellation_while_acknowledgement_is_stalled(self):
        with Process() as process:
            process.send({"id": "x" * 262144, "type": "prompt", "message": "wait"})
            process.read(1)
            process.send({"id": "abort", "type": "abort"})
            process.cancelled_wait()
            values = process.until(lambda item: item["type"] == "agent_end")
            self.assertEqual("response", values[0]["type"])
            self.assertEqual("prompt", values[0]["command"])
            self.assertEqual("aborted", values[-1]["status"])
            self.assertEqual(0, process.idle()["messageCount"])
            process.finish()

    def test_cancellation_drains_partial_text_and_terminal_frames(self):
        for prompt in ("large", "final_large"):
            with self.subTest(prompt=prompt), Process() as process:
                process.send({"type": "prompt", "message": prompt})
                self.assertEqual("response", process.line()["type"])
                self.assertEqual("agent_start", process.line()["type"])
                if not process.buffer:
                    process.read(1)
                process.send({"id": "abort", "type": "abort"})
                process.cancelled_wait()
                values = process.until(lambda item: item["type"] == "agent_end")
                self.assertEqual("aborted" if prompt == "large" else "completed", values[-1]["status"])
                self.assertEqual(0, process.idle()["messageCount"])
                output, _ = process.finish()
                self.assertEqual(b"", output)

    def test_stalled_stdout_has_bounded_eof_and_interrupt_shutdown(self):
        for interrupt in (False, True):
            with self.subTest(interrupt=interrupt), Process() as process:
                process.send({"id": "x" * 262144, "type": "get_state"})
                process.read(1)
                if interrupt:
                    process.child.send_signal(signal.SIGINT)
                else:
                    process.eof()
                process.child.wait(timeout=3)
                _, diagnostics = process.finish(success=False)
                self.assertIn(b"OutputDrainTimeout", diagnostics)

    def test_closed_output_cancels_blocked_input(self):
        with Process() as process:
            process.child.stdout.close()
            process.send({"type": "get_state"})
            process.child.wait(timeout=3)
            _, diagnostics = process.finish(success=False)
            self.assertIn(b"BrokenPipe", diagnostics)

    def test_output_size_error_keeps_session_usable(self):
        with Process() as process:
            process.send({"type": "prompt", "message": "oversize"})
            values = process.until(lambda item: item["type"] == "protocol_error")
            self.assertEqual("output_too_large", values[-1]["error"]["code"])
            self.assertEqual(0, process.idle()["messageCount"])
            self.assertEqual("1", process.prompt("history")[-1]["text"])
            process.idle()
            process.finish()

    def test_queue_limit_includes_the_entire_pending_frame(self):
        with Process() as process:
            command = {"id": "x" * 262144, "type": "get_state"}
            process.send(command)
            process.read(1)
            for _ in range(15):
                process.send(command)
            # Keep stdout stalled until overflow has stopped the command
            # reader and cancellation has reached its sibling waiters.
            process.cancelled_wait()
            output, diagnostics = process.finish(success=False)
            self.assertLessEqual(len(output), 4_194_304)
            frames = [json.loads(line) for line in output.splitlines()]
            self.assertEqual("output_queue_full", frames[-1]["error"]["code"])
            self.assertIn(b"OutputQueueFull", diagnostics)

    def test_exact_input_limit_and_unterminated_final_line(self):
        with Process() as process:
            prefix = b'{"type":"get_state","padding":"'
            suffix = b'"}'
            process.write(prefix + b"x" * (LIMIT - len(prefix) - len(suffix)) + suffix + b"\n")
            self.assertEqual("get_state", process.line()["command"])
            process.write(b'{"type":"get_state"}')
            process.eof()
            self.assertEqual("get_state", process.line()["command"])
            process.finish()

    def test_oversized_input_terminates_before_lf(self):
        with Process() as process:
            process.write(b"x" * (LIMIT + 1))
            self.assertEqual("line_too_large", process.line()["error"]["code"])
            process.finish()

    def test_invalid_utf8_terminates(self):
        with Process() as process:
            process.write(b'"\xc3')
            process.write(b'("\n')
            self.assertEqual("invalid_utf8", process.line()["error"]["code"])
            process.finish()

    def test_malformed_json_and_invalid_shapes_are_recoverable(self):
        with Process() as process:
            for line in (b"{\n", b"\n", b'{}{}\n', b'"\\ud800"\n'):
                process.write(line)
                self.assertEqual("invalid_json", process.line()["error"]["code"])
            for value in (None, [], 5, "command"):
                process.send(value)
                self.assertEqual("invalid_command", process.line()["error"]["code"])
            self.assertEqual(0, process.idle()["messageCount"])
            process.finish()

    def test_held_cleanup_preserves_busy_admission_and_abort_responsiveness(self):
        read_fd, write_fd = os.pipe()
        try:
            with Process(read_fd) as process:
                process.send({"type": "prompt", "message": "hold"})
                process.until(lambda item: item["type"] == "text_delta")
                process.send({"id": "abort", "type": "abort"})
                self.assertTrue(process.line()["data"]["accepted"])
                process.send({"id": "busy", "type": "prompt", "message": "blocked"})
                self.assertEqual("busy", process.line()["error"]["code"])
                process.send({"id": "state", "type": "get_state"})
                self.assertEqual("running", process.line()["data"]["status"])
                os.write(write_fd, b"x")
                self.assertEqual("aborted", process.line()["status"])
                self.assertEqual(0, process.idle()["messageCount"])
                process.finish()
        finally:
            os.close(read_fd)
            os.close(write_fd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
