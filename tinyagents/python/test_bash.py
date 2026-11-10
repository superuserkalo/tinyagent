"""Process-stop errors must reach the caller without escaping a reader thread."""

import os
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from tiny_agent import MAX_TOOL_OUTPUT_BYTES, ToolOutputLimitError, run_bash
from tinyagent import CancellationToken, CancelledError


@unittest.skipUnless(os.name == "posix", "Process groups are a POSIX contract")
class BashStopTests(unittest.TestCase):
    def test_cancellation_stops_descendants_after_the_shell_exits(self):
        token = CancellationToken()
        timer = threading.Timer(0.1, token.cancel)
        started = time.monotonic()
        timer.start()
        try:
            with self.assertRaises(CancelledError):
                run_bash("sleep 3 &", token)
        finally:
            timer.join()
        self.assertLess(time.monotonic() - started, 2)

    def exercise_stop(self, wait_confirms_exit):
        popen = subprocess.Popen
        thread_errors = []

        def start(*args, **kwargs):
            process = popen(*args, **kwargs)
            wait = process.wait
            # Force the poll/kill race independently of scheduler timing.
            process.poll = lambda: None

            def await_exit(timeout=None):
                if timeout is not None and not wait_confirms_exit:
                    raise subprocess.TimeoutExpired(args[0], timeout)
                return wait(timeout=timeout)

            process.wait = await_exit
            return process

        expected = ToolOutputLimitError if wait_confirms_exit else PermissionError
        with (
            patch("tiny_agent.subprocess.Popen", side_effect=start),
            patch(
                "tiny_agent.os.killpg",
                side_effect=PermissionError("cannot signal group"),
            ) as kill,
            patch("threading.excepthook", side_effect=thread_errors.append),
            self.assertRaises(expected),
        ):
            run_bash(f"python3 -c 'print(\"x\" * {MAX_TOOL_OUTPUT_BYTES + 1})'")
        kill.assert_called()
        self.assertEqual(thread_errors, [])

    def test_denied_signal_is_ignored_only_after_confirmed_exit(self):
        self.exercise_stop(wait_confirms_exit=True)

    def test_unconfirmed_stop_failure_reaches_the_caller(self):
        self.exercise_stop(wait_confirms_exit=False)


if __name__ == "__main__":
    unittest.main()
