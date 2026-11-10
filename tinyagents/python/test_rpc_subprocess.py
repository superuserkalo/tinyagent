"""Actual stdin/stdout pipes, process interrupts, and native tool cleanup."""

import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tinyagent import MAX_JSONL_LINE_BYTES
from tiny_agent import __file__ as cli_path


class Lines:
    def __init__(self, stream):
        self.values = []
        self.pending = queue.Queue()
        self.errors = []

        def read():
            try:
                for line in stream:
                    value = json.loads(line)
                    self.values.append(value)
                    self.pending.put(value)
            except BaseException as error:
                self.errors.append(error)

        self.thread = threading.Thread(target=read, daemon=True)
        self.thread.start()

    def until(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while True:
            try:
                value = self.pending.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise AssertionError(
                    f"Pipe message missing; received {repr(self.values[-3:])[:500]}"
                ) from None
            if predicate(value):
                return value

    def join(self):
        self.thread.join(3)
        if self.thread.is_alive():
            raise AssertionError("pipe reader did not finish")
        if self.errors:
            raise self.errors[0]


@unittest.skipUnless(
    os.name == "posix", "Native signals and Bash process groups require POSIX"
)
class RpcSubprocessTests(unittest.TestCase):
    def setUp(self):
        self.readers = {}

    def read_output(self, process):
        output = Lines(process.stdout)
        self.readers[process].append(output)
        return output

    def start(self, *, read_output=True, pid_path="unused", cli=False):
        environment = dict(os.environ)
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            environment.pop(name, None)
        if cli:
            sessions = tempfile.TemporaryDirectory(prefix="ta-rpc-")
            self.addCleanup(sessions.cleanup)
            environment["OPENAI_API_KEY"] = "unused-by-local-state-command"
            environment["TINYAGENT_PROVIDER"] = "openai"
            environment["TINYAGENT_SESSION_DIR"] = sessions.name
        command = (
            [sys.executable, cli_path, "--mode", "rpc"]
            if cli
            else [
                sys.executable,
                str(Path(__file__).with_name("rpc_fixture.py")),
                str(pid_path),
            ]
        )
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        errors = Lines(process.stderr)
        self.readers[process] = [errors]
        output = self.read_output(process) if read_output else None

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            if not process.stdin.closed:
                process.stdin.close()
            for reader in self.readers[process]:
                reader.join()
            process.stdout.close()
            process.stderr.close()

        self.addCleanup(cleanup)
        return process, output, errors

    def send(self, process, **command):
        process.stdin.write(json.dumps(command).encode() + b"\n")
        process.stdin.flush()

    def test_eof_releases_an_unread_stdout_pipe(self):
        process, _, errors = self.start(read_output=False)
        self.send(process, type="prompt", message="flood")
        errors.until(lambda value: value.get("started") == "flood")
        time.sleep(0.05)
        process.stdin.close()
        self.assertEqual(process.wait(timeout=3), 0)
        closed = errors.until(lambda value: value.get("closed") == "flood")
        self.assertTrue(closed["cancelled"])
        self.assertLessEqual(closed["produced"], 2)
        errors.until(lambda value: value.get("driver_closed"))

    def test_abort_and_reset_release_an_unread_stdout_pipe(self):
        for command in ("abort", "new_session"):
            with self.subTest(command=command):
                process, _, errors = self.start(read_output=False)
                self.send(process, type="prompt", message="flood")
                errors.until(lambda value: value.get("started") == "flood")
                self.send(process, id="cancel", type=command)
                self.assertEqual(process.wait(timeout=3), 0)
                self.assertTrue(
                    errors.until(lambda value: value.get("closed") == "flood")[
                        "cancelled"
                    ]
                )

    def test_interrupt_releases_provider_and_stalled_output(self):
        for prompt in ("wait", "flood"):
            with self.subTest(prompt=prompt):
                process, _, errors = self.start(read_output=prompt == "wait")
                self.send(process, type="prompt", message=prompt)
                errors.until(lambda value: value.get("started") == prompt)
                process.send_signal(signal.SIGINT)
                self.assertEqual(process.wait(timeout=3), 0)
                self.assertTrue(
                    errors.until(lambda value: value.get("closed") == prompt)[
                        "cancelled"
                    ]
                )

    def test_output_failure_releases_input_and_provider(self):
        process, _, errors = self.start(read_output=False)
        self.send(process, type="prompt", message="flood")
        errors.until(lambda value: value.get("started") == "flood")
        process.stdout.close()
        self.assertEqual(process.wait(timeout=3), 0)
        self.assertTrue(
            errors.until(lambda value: value.get("closed") == "flood")["cancelled"]
        )
        errors.until(lambda value: "output_error" in value)

    def test_fatal_input_releases_stalled_output(self):
        for invalid in (b"\xff\n", b" " * (MAX_JSONL_LINE_BYTES + 1)):
            with self.subTest(size=len(invalid)):
                process, _, errors = self.start(read_output=False)
                self.send(process, type="prompt", message="flood")
                errors.until(lambda value: value.get("started") == "flood")
                process.stdin.write(invalid)
                process.stdin.flush()
                self.assertEqual(process.wait(timeout=3), 0)
                self.assertTrue(
                    errors.until(lambda value: value.get("closed") == "flood")[
                        "cancelled"
                    ]
                )

    def test_eof_during_stalled_command_reply(self):
        process, _, errors = self.start(read_output=False)
        self.send(process, type="get_state", id="x" * (MAX_JSONL_LINE_BYTES // 2))
        process.stdin.close()
        self.assertEqual(process.wait(timeout=3), 0)
        errors.until(lambda value: value.get("driver_closed"))

    def test_pipelined_commands_do_not_hide_eof_behind_a_stalled_reply(self):
        process, _, errors = self.start(read_output=False)
        self.send(process, type="get_state", id="x" * (MAX_JSONL_LINE_BYTES // 2))
        self.send(process, type="get_state", id="queued")
        process.stdin.close()
        self.assertEqual(process.wait(timeout=3), 0)
        errors.until(lambda value: value.get("driver_closed"))

    def test_abort_is_read_while_a_command_reply_is_stalled(self):
        process, _, errors = self.start(read_output=False)
        self.send(process, type="prompt", message="wait")
        errors.until(lambda value: value.get("started") == "wait")
        self.send(process, type="get_state", id="x" * (MAX_JSONL_LINE_BYTES // 2))
        self.send(process, type="abort", id="abort")
        self.assertEqual(process.wait(timeout=3), 0)
        self.assertTrue(
            errors.until(lambda value: value.get("closed") == "wait")["cancelled"]
        )

    def test_eof_during_prompt_acknowledgement_never_starts_provider(self):
        process, _, errors = self.start(read_output=False)
        self.send(
            process, type="prompt", message="wait", id="x" * (MAX_JSONL_LINE_BYTES // 2)
        )
        process.stdin.close()
        self.assertEqual(process.wait(timeout=3), 0)
        errors.join()
        self.assertFalse([value for value in errors.values if "started" in value])

    def test_oversized_reply_cancels_before_waiting_on_stalled_output(self):
        process, _, errors = self.start(read_output=False)
        self.send(process, type="prompt", message="flood")
        errors.until(lambda value: value.get("started") == "flood")
        self.send(process, type="get_state", id="x" * (MAX_JSONL_LINE_BYTES - 64))
        self.assertEqual(process.wait(timeout=3), 0)
        self.assertTrue(
            errors.until(lambda value: value.get("closed") == "flood")["cancelled"]
        )

    def test_cli_interrupts_while_idle_and_while_output_is_stalled(self):
        for stalled in (False, True):
            with self.subTest(stalled=stalled):
                process, output, errors = self.start(cli=True, read_output=not stalled)
                self.send(
                    process,
                    type="get_state",
                    id="x" * (MAX_JSONL_LINE_BYTES // 2) if stalled else "state",
                )
                if output:
                    output.until(lambda value: value.get("id") == "state")
                else:
                    time.sleep(0.05)
                process.send_signal(signal.SIGINT)
                self.assertEqual(process.wait(timeout=3), 0)
                errors.join()
                self.assertEqual(errors.values, [])

    def test_cancellation_can_drain_and_keep_the_connection_usable(self):
        process, _, errors = self.start(read_output=False)
        self.send(process, type="prompt", message="flood")
        errors.until(lambda value: value.get("started") == "flood")
        self.send(process, id="abort", type="abort")
        time.sleep(0.05)
        output = self.read_output(process)
        output.until(lambda value: value.get("type") == "agent_end")
        self.send(process, id="state", type="get_state")
        state = output.until(lambda value: value.get("id") == "state")
        self.assertEqual(
            (state["data"]["status"], state["data"]["messageCount"]), ("idle", 0)
        )
        self.send(process, id="next", type="prompt", message="next")
        terminal = output.until(lambda value: value.get("type") == "agent_end")
        self.assertEqual(terminal["status"], "completed")
        process.stdin.close()
        self.assertEqual(process.wait(timeout=3), 0)

    def test_eof_and_interrupt_stop_bash_and_its_descendants(self):
        for interrupt in (False, True):
            with self.subTest(interrupt=interrupt), tempfile.TemporaryDirectory() as directory:
                pid_path = Path(directory) / "pids"
                process, output, _ = self.start(pid_path=pid_path)
                self.send(process, type="prompt", message="bash")
                deadline = time.monotonic() + 3
                while not pid_path.exists() or not pid_path.read_text().strip():
                    if time.monotonic() >= deadline:
                        self.fail("Bash did not report its process group")
                    time.sleep(0.01)
                pids = [int(value) for value in pid_path.read_text().split()]
                self.assertEqual(len(pids), 2)

                def stop_group(group=pids[0]):
                    try:
                        os.killpg(group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

                self.addCleanup(stop_group)
                for pid in pids:
                    os.kill(pid, 0)
                if interrupt:
                    process.send_signal(signal.SIGINT)
                else:
                    process.stdin.close()
                self.assertEqual(process.wait(timeout=3), 0)
                output.join()
                events = [value for value in output.values if "sequence" in value]
                self.assertEqual(
                    [value["type"] for value in events],
                    [
                        "agent_start",
                        "tool_execution_start",
                        "tool_execution_end",
                        "agent_end",
                    ],
                )
                self.assertEqual(events[-1]["status"], "aborted")
                for pid in pids:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
