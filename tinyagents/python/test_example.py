"""Runs examples/quickstart.py unchanged against a local fake Responses server."""

import json
import os
import runpy
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tinyagent import Session

EXAMPLE = Path(__file__).with_name("examples") / "quickstart.py"
SUMMARY = "The release still needs a changelog entry and a wheel upload."


def text(value):
    middle = len(value) // 2
    message = {"type": "message", "content": [{"type": "output_text", "text": value}]}
    return [
        {"type": "response.output_text.delta", "delta": value[:middle]},
        {"type": "response.output_text.delta", "delta": value[middle:]},
        {"type": "response.completed", "response": {"output": [message]}},
    ]


def tool_call(call_id):
    call = {"type": "function_call", "id": "item_" + call_id, "call_id": call_id,
            "name": "read_notes", "arguments": '{"topic":"release"}'}
    return [
        {"type": "response.output_item.added", "output_index": 0, "item": call},
        {"type": "response.completed", "response": {"output": [call]}},
    ]


class FakeResponses(BaseHTTPRequestHandler):
    """Answers each request from its content alone, so it works across restarts."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send_events(self, events):
        data = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(request)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        prompt = [item["content"] for item in request["input"] if item.get("role") == "user"][-1]
        after_tool = request["input"][-1].get("type") == "function_call_output"
        if request["instructions"].startswith("Summarize"):
            self.send_events(text(SUMMARY))
        elif "notes" in prompt and not after_tool:
            self.send_events(tool_call(f"call_{len(self.server.requests)}"))
        elif "essay" in prompt:
            # Begin an answer, then keep the stream open until the client leaves.
            self.send_events([{"type": "response.output_text.delta", "delta": "Release management "}])
            deadline = time.monotonic() + 5
            try:
                while time.monotonic() < deadline:
                    self.wfile.write(b"c\r\n: waiting\n\n\r\n")
                    self.wfile.flush()
                    time.sleep(0.02)
            except OSError:
                self.server.client_left.set()
            return
        elif after_tool:
            self.send_events(text("Two items are open: the changelog entry and the wheel upload."))
        elif "first" in prompt:
            self.send_events(text("The changelog entry, because it needs a review."))
        else:
            self.send_events(text("Write and review the changelog, then upload the wheel."))
        self.wfile.write(b"0\r\n\r\n")


class QuickstartExampleTests(unittest.TestCase):
    def test_example_uses_a_tool_cancels_restarts_resumes_compacts_and_continues(self):
        # Binding is mandatory. Restricted environments report failure here.
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeResponses)
        server.requests = []
        server.client_left = threading.Event()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        temp = tempfile.TemporaryDirectory(prefix="ta-example-")
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name) / "sessions"
        environment = {
            **os.environ,
            "OPENAI_API_KEY": "offline",
            "QUICKSTART_ENDPOINT": f"http://127.0.0.1:{server.server_port}/v1/responses",
            "PYTHONPATH": os.pathsep.join(sys.path),
        }
        environment.pop("TINYAGENT_MODEL", None)
        try:
            completed = subprocess.run(
                [sys.executable, str(EXAMPLE), str(directory)], cwd=temp.name, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30,
            )
            self.assertTrue(server.client_left.wait(5), "cancellation did not close the provider request")
        finally:
            server.shutdown()
            thread.join(3)
            server.server_close()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        (identifier,) = Session.list(directory)
        self.assertEqual(completed.stdout.splitlines(), [
            '[tool read_notes {"topic": "release"}]',
            "Two items are open: the changelog entry and the wheel upload.",
            "[run completed]",
            '[tool read_notes {"topic": "release"}]',
            "Release management ",
            "[run aborted]",
            "cancelled; history still has 4 messages",
            f"session {identifier} saved in {directory}; restarting",
            f"resumed {identifier} with 4 messages",
            "The changelog entry, because it needs a review.",
            "[run completed]",
            "[compaction completed]",
            "Write and review the changelog, then upload the wheel.",
            "[run completed]",
            "final answer: Write and review the changelog, then upload the wheel.",
            "8 messages in history, 19 records on disk",
        ])

        # The provider saw the tool result, a tool-free summary request, and
        # then the summary in place of the turns it covers.
        requests = server.requests
        self.assertEqual(len(requests), 7)
        self.assertEqual(requests[1]["input"][-1]["type"], "function_call_output")
        self.assertIn("Still open", requests[1]["input"][-1]["output"])
        self.assertTrue(requests[5]["instructions"].startswith("Summarize"))
        self.assertEqual(requests[5]["tools"], [])
        final = [item["content"] for item in requests[6]["input"] if item.get("role") == "user"]
        self.assertEqual(final, ["Earlier conversation summary:\n" + SUMMARY,
                                 "Which open item comes first?", "Give me the plan in one sentence."])
        self.assertFalse(any("essay" in json.dumps(request["input"]) for request in requests[4:]))

        # A third process can still read everything, including the cancelled run.
        quickstart = runpy.run_path(str(EXAMPLE))
        with patch.dict(os.environ, environment):
            agent = quickstart["build_agent"]()
        with Session.open(agent, directory, identifier, budget=quickstart["BUDGET"]) as session:
            self.assertFalse(session.recovery_required)
            self.assertEqual(session.recovery_notes, [])
            kinds = [record["type"] for record in session.records]
            self.assertEqual([kinds.count(kind) for kind in
                              ("run_start", "run_commit", "run_failed", "tool_intent", "tool_result",
                               "reconciled", "checkpoint")],
                             [4, 3, 1, 2, 2, 0, 1])
            self.assertEqual([item["role"] for item in session.history],
                             ["user", "assistant", "tool_result", "assistant",
                              "user", "assistant", "user", "assistant"])


if __name__ == "__main__":
    unittest.main()
