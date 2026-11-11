import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tiny_agent import __file__ as cli_path, run_bash
from tinyagent import (
    Agent,
    AnthropicProvider,
    CancellationToken,
    MAX_HTTP_BODY_BYTES,
    MAX_JSONL_LINE_BYTES,
    MAX_QUEUED_OUTPUT_BYTES,
    MAX_TOOL_OUTPUT_BYTES,
    PROTOCOL_VERSION,
    CancelledError,
    OpenAIProvider,
    ProviderError,
    ProviderEvent,
    ProviderTurn,
    RpcSession,
    Tool,
    ToolOutputLimitError,
    run_rpc,
)


def provider_from_turns(turns):
    remaining = list(turns)

    def event_transport(payload, cancellation):
        del payload
        yield from remaining.pop(0)

    return OpenAIProvider(event_transport=event_transport)


def message_events(*deltas):
    text = "".join(deltas)
    item = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }
    return [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**item, "content": []},
        },
        *(
            {"type": "response.output_text.delta", "output_index": 0, "delta": delta}
            for delta in deltas
        ),
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": {}},
    ]


class PackagingTests(unittest.TestCase):
    def test_public_package_uses_canonical_agent(self):
        self.assertEqual(Agent.__module__, "tiny_agent")


class TinyAgentTests(unittest.TestCase):
    def test_compatible_complete_transport_tool_loop(self):
        responses = [
            [
                {"type": "reasoning", "id": "reasoning_1", "summary": []},
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "echo",
                    "arguments": '{"text":"hello"}',
                },
            ],
            [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Done."}],
                }
            ],
        ]
        requests = []

        def transport(payload):
            requests.append(copy.deepcopy(payload))
            return responses.pop(0)

        echo = Tool(
            name="echo",
            description="Return text.",
            parameters={"type": "object"},
            handler=lambda arguments: arguments["text"],
        )
        result = Agent(model="test", tools=[echo], transport=transport).run(
            "Echo hello"
        )

        self.assertEqual(result.text, "Done.")
        self.assertEqual(
            [item["role"] for item in result.history],
            ["user", "assistant", "tool_result", "assistant"],
        )
        self.assertEqual(result.history[-2]["callId"], "call_1")
        self.assertEqual(result.history[-2]["output"], "hello")
        self.assertEqual(
            [item.get("type", item.get("role")) for item in requests[-1]["input"]],
            ["user", "reasoning", "function_call", "function_call_output"],
        )

    def test_text_deltas_buffered_arguments_and_sequential_tool_events(self):
        function_events = []
        for index, (call_id, text) in enumerate((("call_1", "one"), ("call_2", "two"))):
            function_events.extend(
                [
                    {
                        "type": "response.output_item.added",
                        "output_index": index,
                        "item": {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": "echo",
                            "arguments": "",
                        },
                    },
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": index,
                        "delta": '{"text":',
                    },
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": index,
                        "delta": f'"{text}"}}',
                    },
                    {
                        "type": "response.output_item.done",
                        "output_index": index,
                        "item": {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": "echo",
                            "arguments": "",
                        },
                    },
                ]
            )
        function_events.append({"type": "response.completed", "response": {}})
        provider = provider_from_turns([function_events, message_events("Do", "ne.")])
        calls = []

        def echo(arguments, cancellation):
            self.assertFalse(cancellation.cancelled)
            calls.append(arguments["text"])
            if arguments["text"] == "two":
                raise RuntimeError("boom")
            return arguments["text"]

        events = []
        result = Agent(
            model="test",
            provider=provider,
            tools=[Tool("echo", "Return text.", {"type": "object"}, echo)],
        ).run("go", on_event=events.append)

        self.assertEqual(result.text, "Done.")
        self.assertEqual(calls, ["one", "two"])
        self.assertEqual(
            [event.type for event in events],
            [
                "agent_start",
                "tool_execution_start",
                "tool_execution_end",
                "tool_execution_start",
                "tool_execution_end",
                "text_delta",
                "text_delta",
                "agent_end",
            ],
        )
        tool_ends = [event for event in events if event.type == "tool_execution_end"]
        self.assertEqual([event.data["isError"] for event in tool_ends], [False, True])
        self.assertIn("RuntimeError: boom", result.history[-2]["output"])
        self.assertEqual(
            [event.sequence for event in events], list(range(1, len(events) + 1))
        )
        self.assertEqual(len({event.run_id for event in events}), 1)

    def test_malformed_arguments_emit_failed_terminal_event(self):
        events = [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "call_id": "c",
                    "name": "echo",
                    "arguments": "",
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "delta": '{"text":',
            },
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "call_id": "c",
                    "name": "echo",
                    "arguments": "",
                },
            },
            {"type": "response.completed", "response": {}},
        ]
        emitted = []
        agent = Agent(model="test", provider=provider_from_turns([events]))
        with self.assertRaisesRegex(ProviderError, "invalid arguments"):
            agent.run("go", on_event=emitted.append)
        self.assertEqual(
            [event.type for event in emitted], ["agent_start", "agent_end"]
        )
        self.assertEqual(emitted[-1].data["status"], "failed")

    def test_provider_failure_emit_failed_terminal_event(self):
        provider = provider_from_turns(
            [
                [
                    {
                        "type": "response.output_text.delta",
                        "output_index": 0,
                        "delta": "partial",
                    },
                    {
                        "type": "response.failed",
                        "response": {"error": {"message": "upstream exploded"}},
                    },
                ]
            ]
        )
        emitted = []
        with self.assertRaisesRegex(ProviderError, "upstream exploded"):
            Agent(model="test", provider=provider).run("go", on_event=emitted.append)
        self.assertEqual(
            [event.type for event in emitted],
            ["agent_start", "text_delta", "agent_end"],
        )
        self.assertEqual(emitted[-1].data["status"], "failed")

    def test_run_scoped_cancellation_emit_aborted_terminal_event(self):
        token = CancellationToken()

        def event_transport(payload, cancellation):
            del payload, cancellation
            yield {
                "type": "response.output_text.delta",
                "output_index": 0,
                "delta": "partial",
            }
            token.cancel()
            yield {"type": "response.completed", "response": {"output": []}}

        emitted = []
        agent = Agent(
            model="test", provider=OpenAIProvider(event_transport=event_transport)
        )
        with self.assertRaises(CancelledError):
            agent.run("go", cancellation=token, on_event=emitted.append)
        self.assertEqual(
            [event.type for event in emitted],
            ["agent_start", "text_delta", "agent_end"],
        )
        self.assertEqual(emitted[-1].data["status"], "aborted")

    def test_anthropic_streaming_tools_and_lossless_replay(self):
        turns = [
            [
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_tool",
                        "model": "claude-test",
                        "role": "assistant",
                        "content": [],
                        "usage": {"input_tokens": 4},
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "thinking",
                        "thinking": "",
                        "signature": "",
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "Use echo."},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "signature_delta", "signature": "sig_123"},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "tool_1",
                        "name": "echo",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '{"text":'},
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '"hello"}'},
                },
                {"type": "content_block_stop", "index": 1},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 8},
                },
                {"type": "message_stop"},
            ],
            [
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_text",
                        "model": "claude-test",
                        "usage": {"input_tokens": 9},
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Done."},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 2},
                },
                {"type": "message_stop"},
            ],
        ]
        requests = []

        def event_transport(payload, cancellation):
            self.assertFalse(cancellation.cancelled)
            requests.append(copy.deepcopy(payload))
            yield from turns.pop(0)

        echo = Tool(
            "echo",
            "Return text.",
            {"type": "object"},
            lambda arguments: arguments["text"],
        )
        emitted = []
        result = Agent(
            model="claude-test",
            instructions="Be concise.",
            tools=[echo],
            provider=AnthropicProvider(event_transport=event_transport, max_tokens=123),
        ).run("Echo hello", on_event=emitted.append)

        self.assertEqual(result.text, "Done.")
        self.assertEqual(
            [event.type for event in emitted],
            [
                "agent_start",
                "tool_execution_start",
                "tool_execution_end",
                "text_delta",
                "agent_end",
            ],
        )
        self.assertEqual(requests[0]["system"], "Be concise.")
        self.assertEqual(requests[0]["max_tokens"], 123)
        self.assertEqual(requests[0]["tools"][0]["input_schema"], {"type": "object"})
        replay = requests[1]["messages"]
        self.assertEqual(
            [message["role"] for message in replay], ["user", "assistant", "user"]
        )
        self.assertEqual(
            replay[1]["content"][0],
            {
                "type": "thinking",
                "thinking": "Use echo.",
                "signature": "sig_123",
            },
        )
        self.assertEqual(replay[1]["content"][1]["input"], {"text": "hello"})
        self.assertEqual(
            replay[2]["content"][0],
            {
                "type": "tool_result",
                "tool_use_id": "tool_1",
                "content": "hello",
                "is_error": False,
            },
        )
        self.assertEqual(
            result.history[1]["providerMetadata"]["provider"], "anthropic-messages"
        )
        self.assertEqual(
            result.history[1]["providerMetadata"]["message"]["usage"],
            {
                "input_tokens": 4,
                "output_tokens": 8,
            },
        )

    def test_anthropic_malformed_arguments_and_provider_error(self):
        malformed = [
            {"type": "message_start", "message": {"id": "m", "model": "claude-test"}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "t",
                    "name": "echo",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"text":'},
            },
            {"type": "content_block_stop", "index": 0},
        ]
        emitted = []
        with self.assertRaisesRegex(ProviderError, "invalid arguments"):
            Agent(
                model="claude-test",
                provider=AnthropicProvider(
                    event_transport=lambda payload, cancellation: iter(malformed)
                ),
            ).run("go", on_event=emitted.append)
        self.assertEqual(emitted[-1].data["status"], "failed")

        failed = [
            {
                "type": "error",
                "error": {"type": "overloaded_error", "message": "overloaded"},
            }
        ]
        with self.assertRaisesRegex(ProviderError, "overloaded"):
            Agent(
                model="claude-test",
                provider=AnthropicProvider(
                    event_transport=lambda payload, cancellation: iter(failed)
                ),
            ).run("go")

    def test_anthropic_cancellation(self):
        token = CancellationToken()

        def event_transport(payload, cancellation):
            del payload, cancellation
            yield {"type": "message_start", "message": {"id": "m", "content": []}}
            yield {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            }
            yield {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "partial"},
            }
            token.cancel()
            yield {"type": "content_block_stop", "index": 0}

        emitted = []
        with self.assertRaises(CancelledError):
            Agent(
                model="claude-test",
                provider=AnthropicProvider(event_transport=event_transport),
            ).run("go", cancellation=token, on_event=emitted.append)
        self.assertEqual(
            [event.type for event in emitted],
            ["agent_start", "text_delta", "agent_end"],
        )
        self.assertEqual(emitted[-1].data["status"], "aborted")

    def test_bash_process_is_cancelled(self):
        token = CancellationToken()
        timer = threading.Timer(0.05, token.cancel)
        started = time.monotonic()
        timer.start()
        with self.assertRaises(CancelledError):
            run_bash("sleep 5", token)
        timer.join()
        self.assertLess(time.monotonic() - started, 2)


class ImportTests(unittest.TestCase):
    def test_import_prints_nothing_starts_nothing_and_ignores_the_environment(self):
        code = (
            "import threading, tinyagent, tiny_agent\n"
            "assert tiny_agent.MODEL == 'gpt-5.6', tiny_agent.MODEL\n"
            "assert threading.active_count() == 1\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "TINYAGENT_MODEL": "from-environment",
                "PYTHONPATH": os.pathsep.join(sys.path),
            },
            timeout=30,
        )
        self.assertEqual(
            (completed.returncode, completed.stdout, completed.stderr), (0, b"", b"")
        )


class RpcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_path = Path(__file__).parents[2] / "conformance" / "rpc_v1.json"
        cls.fixture = json.loads(fixture_path.read_text())

    def test_shared_limits_and_static_jsonl_fixtures(self):
        self.assertEqual(PROTOCOL_VERSION, 2)
        if self.fixture["protocolVersion"] == 1:
            # conformance/rpc_v1.json still describes version 1. Version 2 adds
            # three get_state fields; apply them until the shared fixture does.
            self.fixture["scenarios"][0]["expected"][0]["data"].update(
                protocolVersion=2, resumable=False, paused=None, usage=None)
        self.assertEqual(
            self.fixture["limits"],
            {
                "jsonlLineBytes": MAX_JSONL_LINE_BYTES,
                "httpBodyBytes": MAX_HTTP_BODY_BYTES,
                "queuedOutputBytes": MAX_QUEUED_OUTPUT_BYTES,
                "toolOutputBytes": MAX_TOOL_OUTPUT_BYTES,
            },
        )
        scenarios = self.fixture["scenarios"][:6]
        commands = [
            command for scenario in scenarios for command in scenario["commands"]
        ]
        source = io.BytesIO(
            b"".join(
                json.dumps(command, separators=(",", ":")).encode() + b"\n"
                for command in commands
            )
            + b"{\n"
        )
        target = io.BytesIO()
        run_rpc(Agent(model="test", transport=lambda payload: []), source, target)
        actual = [json.loads(line) for line in target.getvalue().splitlines()]
        expected = [item for scenario in scenarios for item in scenario["expected"]]
        expected.append(
            {
                "type": "protocol_error",
                "error": {
                    "code": "invalid_json",
                    "message": "RPC input is not valid JSON",
                },
            }
        )
        self.assertEqual(actual, expected)

    def test_rpc_cli_subprocess_framing_and_fatal_input(self):
        sessions = tempfile.TemporaryDirectory(prefix="ta-rpc-")
        self.addCleanup(sessions.cleanup)

        def invoke(data):
            return subprocess.run(
                [sys.executable, cli_path, "--mode", "rpc"],
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, "OPENAI_API_KEY": "test",
                     "TINYAGENT_PROVIDER": "openai",
                     "TINYAGENT_SESSION_DIR": sessions.name},
                timeout=5,
                check=True,
            )

        completed = invoke(b'{"id":"state","type":"get_state"}\n')
        lines = completed.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["data"]["status"], "idle")
        self.assertEqual(completed.stderr, b"")
        oversized = invoke(b" " * (MAX_JSONL_LINE_BYTES + 1))
        self.assertEqual(
            json.loads(oversized.stdout)["error"]["code"], "line_too_large"
        )
        invalid = invoke(b"\xff\n")
        self.assertEqual(json.loads(invalid.stdout)["error"]["code"], "invalid_utf8")

    def test_rpc_busy_abort_and_rollback(self):
        started = threading.Event()

        class BlockingProvider:
            def stream(self, model, instructions, history, tools, cancellation):
                del model, instructions, history, tools
                yield ProviderEvent("text_delta", "waiting")
                started.set()
                while not cancellation.cancelled:
                    time.sleep(0.01)
                cancellation.raise_if_cancelled()

        emitted = []
        emit_lock = threading.Lock()

        def emit(value):
            with emit_lock:
                emitted.append(copy.deepcopy(value))

        session = RpcSession(Agent(model="test", provider=BlockingProvider()), emit)
        session.handle({"id": "first", "type": "prompt", "message": "wait"})
        self.assertTrue(started.wait(1))
        session.handle({"id": "second", "type": "prompt", "message": "busy"})
        session.handle({"id": "abort", "type": "abort"})
        self.assertTrue(session.wait_idle(1))
        session.handle({"id": "state", "type": "get_state"})

        self.assertEqual(emitted[0]["command"], "prompt")
        run_id = emitted[0]["data"]["runId"]
        self.assertEqual(emitted[1]["type"], "agent_start")
        self.assertEqual(emitted[1]["runId"], run_id)
        busy = next(item for item in emitted if item.get("id") == "second")
        self.assertEqual(busy["error"]["code"], "busy")
        abort = next(item for item in emitted if item.get("id") == "abort")
        self.assertEqual(abort["data"], {"accepted": True, "runId": run_id})
        terminal = next(item for item in emitted if item.get("type") == "agent_end")
        self.assertEqual(terminal["status"], "aborted")
        state = emitted[-1]["data"]
        self.assertEqual((state["status"], state["messageCount"]), ("idle", 0))

    def test_rpc_history_commit_failure_rollback_and_new_session(self):
        class SequencedProvider:
            def __init__(self):
                self.turn = 0

            def stream(self, model, instructions, history, tools, cancellation):
                del model, instructions, history, tools
                cancellation.raise_if_cancelled()
                self.turn += 1
                if self.turn == 2:
                    yield ProviderEvent("text_delta", "partial")
                    raise ProviderError("failed")
                if False:
                    yield ProviderEvent("text_delta", "")
                text = f"answer {self.turn}"
                assistant = {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                }
                return ProviderTurn(text, assistant, [], [])

        emitted = []
        session = RpcSession(
            Agent(model="test", provider=SequencedProvider()),
            emitted.append,
            lambda message: None,
        )
        session.handle({"id": "one", "type": "prompt", "message": "first"})
        self.assertTrue(session.wait_idle(1))
        session.handle({"id": "state1", "type": "get_state"})
        self.assertEqual(emitted[-1]["data"]["messageCount"], 2)
        session.handle({"id": "two", "type": "prompt", "message": "second"})
        self.assertTrue(session.wait_idle(1))
        session.handle({"id": "state2", "type": "get_state"})
        self.assertEqual(emitted[-1]["data"]["messageCount"], 2)
        failed = [
            item
            for item in emitted
            if item.get("type") == "agent_end" and item.get("status") == "failed"
        ]
        self.assertEqual(len(failed), 1)
        session.handle({"id": "new", "type": "new_session"})
        session.handle({"id": "state3", "type": "get_state"})
        self.assertEqual(emitted[-1]["data"]["messageCount"], 0)

    def test_rpc_new_session_aborts_active_run_and_clears_history(self):
        started = threading.Event()

        class WaitingProvider:
            def stream(self, model, instructions, history, tools, cancellation):
                del model, instructions, history, tools
                started.set()
                while not cancellation.cancelled:
                    time.sleep(0.001)
                cancellation.raise_if_cancelled()
                if False:
                    yield ProviderEvent("text_delta", "")

        emitted = []
        session = RpcSession(
            Agent(model="test", provider=WaitingProvider()), emitted.append
        )
        session.handle({"id": "prompt", "type": "prompt", "message": "wait"})
        self.assertTrue(started.wait(1))
        session.handle({"id": "new", "type": "new_session"})
        session.handle({"id": "state", "type": "get_state"})
        response = next(item for item in emitted if item.get("id") == "new")
        self.assertEqual(response["data"], {"abortedRun": True})
        self.assertEqual(
            next(item for item in emitted if item.get("type") == "agent_end")["status"],
            "aborted",
        )
        self.assertEqual(emitted[-1]["data"]["messageCount"], 0)

    def test_rpc_eof_cancels_active_run_cleanly(self):
        class WaitingProvider:
            def stream(self, model, instructions, history, tools, cancellation):
                del model, instructions, history, tools
                while not cancellation.cancelled:
                    time.sleep(0.001)
                cancellation.raise_if_cancelled()
                if False:
                    yield ProviderEvent("text_delta", "")

        target = io.BytesIO()
        run_rpc(
            Agent(model="test", provider=WaitingProvider()),
            io.BytesIO(b'{"id":"prompt","type":"prompt","message":"wait"}\n'),
            target,
        )
        output = [json.loads(line) for line in target.getvalue().splitlines()]
        self.assertEqual(output[0]["command"], "prompt")
        self.assertEqual(output[-1]["status"], "aborted")

    def test_rpc_oversized_output_is_structured_and_cancels_run(self):
        output_error = threading.Event()

        class DelayedEof:
            first = True

            def readline(self, limit):
                del limit
                if self.first:
                    self.first = False
                    return b'{"id":"prompt","type":"prompt","message":"large"}\n'
                self.assert_output()
                return b""

            def assert_output(self):
                if not output_error.wait(1):
                    raise AssertionError("timed out waiting for output limit")

        class SignalingOutput(io.BytesIO):
            def write(self, data):
                result = super().write(data)
                if b'"code":"output_too_large"' in data:
                    output_error.set()
                return result

        class LargeProvider:
            def stream(self, model, instructions, history, tools, cancellation):
                del model, instructions, history, tools, cancellation
                yield ProviderEvent("text_delta", "x" * MAX_JSONL_LINE_BYTES)

        target = SignalingOutput()
        run_rpc(Agent(model="test", provider=LargeProvider()), DelayedEof(), target)
        output = [json.loads(line) for line in target.getvalue().splitlines()]
        self.assertEqual(output[0]["command"], "prompt")
        self.assertIn(
            "output_too_large", [item.get("error", {}).get("code") for item in output]
        )

    def test_rpc_fatal_input_limits(self):
        for source, code in (
            (b" " * (MAX_JSONL_LINE_BYTES + 1), "line_too_large"),
            (b"\xff\n", "invalid_utf8"),
        ):
            target = io.BytesIO()
            run_rpc(
                Agent(model="test", transport=lambda payload: []),
                io.BytesIO(source),
                target,
            )
            output = json.loads(target.getvalue().splitlines()[0])
            self.assertEqual(output["error"]["code"], code)

    def test_tool_output_limit_is_a_recoverable_tool_error(self):
        responses = [
            [
                {
                    "type": "function_call",
                    "call_id": "large",
                    "name": "large",
                    "arguments": "{}",
                }
            ],
            [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "recovered"}],
                }
            ],
        ]
        emitted = []
        result = Agent(
            model="test",
            tools=[
                Tool(
                    "large",
                    "Large output.",
                    {"type": "object"},
                    lambda arguments: "x" * (MAX_TOOL_OUTPUT_BYTES + 1),
                )
            ],
            transport=lambda payload: responses.pop(0),
        ).run("go", on_event=emitted.append)
        tool_end = next(
            event for event in emitted if event.type == "tool_execution_end"
        )
        self.assertTrue(tool_end.data["isError"])
        self.assertIn("ToolOutputLimitError", tool_end.data["output"])
        self.assertLess(len(tool_end.data["output"]), 200)
        self.assertEqual(result.text, "recovered")

    def test_bash_output_limit(self):
        # Version 2 (A6): large Bash output is shortened in the middle, not an error.
        output = run_bash(f"python3 -c 'print(\"x\" * {MAX_TOOL_OUTPUT_BYTES + 1})'")
        self.assertIn(f"\n[... {MAX_TOOL_OUTPUT_BYTES + 2 - 30_000} bytes elided ...]\n", output)
        self.assertLess(len(output), 31_000)


if __name__ == "__main__":
    unittest.main()
