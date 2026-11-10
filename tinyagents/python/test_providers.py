"""Shared SSE bytes through Python's native HTTP response parser and agent loop."""

import http.client
import io
import json
import os
import socket
import threading
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from tinyagent import (
    Agent,
    AnthropicProvider,
    CancellationToken,
    CancelledError,
    OpenAIProvider,
    ProviderError,
    Tool,
)
from tinyagent_sse import MAX_SSE_BYTES

FIXTURES = Path(__file__).parents[2] / "conformance/providers"


def fixture(anthropic, tool=False):
    name = ("anthropic" if anthropic else "openai") + (
        "_tool.sse" if tool else "_text.sse"
    )
    return (FIXTURES / name).read_bytes()


def wire(data, *, status=200, complete=True):
    ending = b"0\r\n\r\n" if complete else b""
    chunk = f"{len(data):x}\r\n".encode() + data + b"\r\n" if data else b""
    return (
        f"HTTP/1.1 {status} Fixture\r\nContent-Type: text/event-stream\r\n".encode()
        + b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
        + chunk
        + ending
    )


class FragmentedInput(io.RawIOBase):
    def __init__(self, data, *, hold=False, chunk_size=1):
        self.data = io.BytesIO(data)
        self.hold = hold
        self.chunk_size = chunk_size
        self.stopped = threading.Event()
        self.waiting = threading.Event()

    def readable(self):
        return True

    def readinto(self, output):
        data = self.data.read(min(len(output), self.chunk_size))
        if not data and self.hold and not self.stopped.is_set():
            self.waiting.set()
            if not self.stopped.wait(3):
                raise TimeoutError("fixture reader was not released")
        output[: len(data)] = data
        return len(data)


class FixtureSocket:
    def __init__(self, data, **options):
        self.source = FragmentedInput(data, **options)
        self.sent = bytearray()
        self.closed = False
        self.stopped = False

    def makefile(self, mode):
        assert mode == "rb"
        return io.BufferedReader(self.source)

    def sendall(self, data):
        self.sent.extend(data)

    def settimeout(self, timeout):
        self.timeout = timeout

    def shutdown(self, how):
        assert how == socket.SHUT_RDWR
        self.stopped = True
        self.source.stopped.set()

    def close(self):
        self.closed = True

    def request(self):
        headers, body = bytes(self.sent).split(b"\r\n\r\n", 1)
        return headers.decode(), json.loads(body)


class FixtureConnection(http.client.HTTPConnection):
    def __init__(self, host, port, *, timeout, stream):
        super().__init__(host, port, timeout=timeout)
        self.stream = stream

    def connect(self):
        self.sock = self.stream


class ProviderTests(unittest.TestCase):
    def test_complete_transport_requests_encrypted_reasoning(self):
        requests = []

        def transport(payload):
            requests.append(payload)
            return []

        Agent(model="fixture", transport=transport).run("go")
        self.assertEqual(len(requests), 1)
        self.assertFalse(requests[0]["store"])
        self.assertEqual(requests[0]["include"], ["reasoning.encrypted_content"])

    def patch_connections(self, streams):
        remaining = iter(streams)

        def connect(host, port, *, timeout):
            return FixtureConnection(
                host, port, timeout=timeout, stream=next(remaining)
            )

        return patch("tinyagent_sse.http.client.HTTPConnection", side_effect=connect)

    def provider(self, anthropic=False, **options):
        provider = AnthropicProvider if anthropic else OpenAIProvider
        return provider(
            api_key="fixture-key",
            endpoint="http://fixture.invalid/events?fixture=1",
            **options,
        )

    def run_bytes(self, data, *, anthropic=False, chunk_size=8192):
        stream = FixtureSocket(wire(data), chunk_size=chunk_size)
        emitted = []
        with self.patch_connections([stream]):
            result = Agent(model="fixture", provider=self.provider(anthropic)).run(
                "go", on_event=emitted.append
            )
        self.assertTrue(stream.closed)
        return result, emitted

    def test_shared_native_tool_loops_and_replay(self):
        for anthropic in (False, True):
            with self.subTest(anthropic=anthropic):
                streams = [
                    FixtureSocket(wire(fixture(anthropic, tool)))
                    for tool in (True, False)
                ]
                calls = []

                def echo(arguments, cancellation):
                    self.assertFalse(cancellation.cancelled)
                    calls.append(arguments["text"])
                    return arguments["text"]

                tool = Tool(
                    "echo",
                    "Return text",
                    {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                    echo,
                )
                events = []
                with self.patch_connections(streams):
                    result = Agent(
                        model="fixture",
                        instructions="Fixture instructions",
                        tools=[tool],
                        provider=self.provider(anthropic),
                    ).run("echo café", on_event=events.append)
                self.assertEqual(result.text, "done\ncafé")
                self.assertEqual(calls, ["café"])
                self.assertEqual(
                    [event.sequence for event in events],
                    list(range(1, len(events) + 1)),
                )
                self.assertEqual(len({event.run_id for event in events}), 1)
                self.assertEqual(
                    [event.type for event in events if event.type.startswith("tool_")],
                    ["tool_execution_start", "tool_execution_end"],
                )
                self.assertEqual(events[-1].data["status"], "completed")
                self.assertTrue(all(stream.closed for stream in streams))
                self.assertTrue(all(stream.source.closed for stream in streams))
                headers, initial = streams[0].request()
                self.assertIn("POST /events?fixture=1 HTTP/1.1", headers)
                self.assertEqual(initial["model"], "fixture")
                self.assertTrue(initial["stream"])
                followup = streams[1].request()[1]
                if anthropic:
                    self.assertIn("x-api-key: fixture-key", headers)
                    self.assertIn("anthropic-version: 2023-06-01", headers)
                    self.assertEqual(initial["system"], "Fixture instructions")
                    self.assertEqual(
                        initial["tools"][0]["input_schema"], tool.parameters
                    )
                    metadata = result.history[1]["providerMetadata"]["message"]
                    self.assertEqual(metadata["fixture_metadata"], "keep")
                    self.assertEqual(
                        metadata["usage"], {"input_tokens": 8, "output_tokens": 12}
                    )
                    self.assertEqual(
                        followup["messages"][1]["content"], metadata["content"]
                    )
                    self.assertEqual(
                        metadata["content"][0]["signature"], "signed-fixture"
                    )
                    self.assertEqual(
                        followup["messages"][2]["content"][0]["content"], "café"
                    )
                else:
                    self.assertIn("Authorization: Bearer fixture-key", headers)
                    self.assertFalse(initial["store"])
                    for payload in (initial, followup):
                        self.assertEqual(
                            payload.get("include"), ["reasoning.encrypted_content"]
                        )
                    self.assertFalse(initial["parallel_tool_calls"])
                    self.assertEqual(initial["tools"][0]["parameters"], tool.parameters)
                    self.assertEqual(
                        followup["input"][1]["encrypted_content"], "opaque-fixture"
                    )
                    self.assertEqual(
                        json.loads(followup["input"][2]["arguments"]), {"text": "café"}
                    )
                    self.assertEqual(followup["input"][3]["output"], "café")

    def test_sse_crlf_final_line_and_split_utf8(self):
        for anthropic in (False, True):
            for data in (
                fixture(anthropic).replace(b"\n", b"\r\n"),
                fixture(anthropic).rstrip(),
            ):
                with self.subTest(anthropic=anthropic, crlf=b"\r\n" in data):
                    result, events = self.run_bytes(
                        data, anthropic=anthropic, chunk_size=1
                    )
                    self.assertEqual(result.text, "done\ncafé")
                    self.assertEqual(
                        "".join(
                            event.data["delta"]
                            for event in events
                            if event.type == "text_delta"
                        ),
                        result.text,
                    )

    def test_sse_rejects_bad_encoding_json_and_limits(self):
        for data in (
            b":\xc3(\n",
            b"data: {}\n\n",
            b"data: {} {}\n\n",
            b"data: {",
            b"data: [DONE]\n\n",
            b'data: {"type":NaN}\n\n',
            b":" + b"x" * MAX_SSE_BYTES + b"\n",
            b"data: "
            + b"x" * (MAX_SSE_BYTES // 2)
            + b"\ndata: "
            + b"x" * (MAX_SSE_BYTES // 2)
            + b"\n\n",
        ):
            with self.subTest(prefix=data[:30], length=len(data)):
                with self.assertRaises(ProviderError):
                    self.run_bytes(data)

    def test_openai_rejects_incomplete_and_malformed_calls(self):
        item = {
            "type": "function_call",
            "call_id": "c",
            "name": "echo",
            "arguments": "{}",
        }
        invalid = [
            {"type": "response.failed"},
            {
                "type": "response.completed",
                "response": {"status": "incomplete", "output": []},
            },
            {"type": "response.completed", "response": {"output": [None]}},
            {"type": "response.completed", "response": {"output": [item, item]}},
            {"type": "response.output_item.added", "output_index": True, "item": item},
            {"type": "response.output_text.delta", "delta": 3},
        ]
        for arguments in ("[]", '{"x": NaN}', "{", ""):
            invalid.append(
                {
                    "type": "response.completed",
                    "response": {"output": [{**item, "arguments": arguments}]},
                }
            )
        for event in invalid:
            with self.subTest(event=event):
                with self.assertRaises(ProviderError):
                    self.run_bytes(b"data: " + json.dumps(event).encode() + b"\n\n")

    def test_anthropic_rejects_truncation_bad_order_and_duplicate_calls(self):
        original = fixture(True, True)
        duplicate = (
            b'data: {"type":"content_block_start","index":2,"content_block":{"type":"tool_use","id":"call_1","name":"echo","input":{}}}\n\n'
            b'data: {"type":"content_block_stop","index":2}\n\n'
        )
        for data in (
            original.replace(
                b'"stop_reason":"tool_use"', b'"stop_reason":"max_tokens"'
            ),
            original.replace(
                b'"stop_reason":"tool_use"', b'"stop_reason":"pause_turn"'
            ),
            original.replace(b'"index":1', b'"index":-1'),
            original.replace(b'"type":"input_json_delta"', b'"type":"text_delta"'),
            original.replace(b'data: {"type":"content_block_stop","index":1}\n\n', b""),
            original.replace(
                b'data: {"type":"message_stop"}',
                duplicate + b'data: {"type":"message_stop"}',
            ),
            original.split(b'data: {"type":"message_delta"')[0]
            + b'data: {"type":"message_stop"}\n\n',
            b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
        ):
            with self.subTest(length=len(data)):
                with self.assertRaises(ProviderError):
                    self.run_bytes(data, anthropic=True)

    def test_argument_limit_applies_across_events(self):
        for anthropic in (False, True):
            data = fixture(anthropic, True)
            # Every event remains small enough individually.
            value = "é" * (MAX_SSE_BYTES // 4)
            event = (
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": value},
                }
                if anthropic
                else {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 1,
                    "delta": value,
                }
            )
            delta = b"data: " + json.dumps(event, ensure_ascii=False).encode() + b"\n\n"
            marker = (
                b'data: {"type":"content_block_delta","index":1'
                if anthropic
                else b'data: {"type":"response.function_call_arguments.delta"'
            )
            data = data[: data.index(marker)] + delta * 3
            with self.subTest(anthropic=anthropic):
                with self.assertRaisesRegex(ProviderError, "arguments exceed"):
                    self.run_bytes(data, anthropic=anthropic)

    def test_openai_sparse_output_index_preserves_arguments(self):
        events = [
            {
                "type": "response.output_item.added",
                "output_index": 7,
                "item": {
                    "type": "function_call",
                    "call_id": "c",
                    "name": "echo",
                    "arguments": "",
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 7,
                "delta": '{"text":"café"}',
            },
            {"type": "response.completed", "response": {}},
        ]
        data = b"".join(
            b"data: " + json.dumps(event).encode() + b"\n\n" for event in events
        )
        streams = [FixtureSocket(wire(data)), FixtureSocket(wire(fixture(False)))]
        with self.patch_connections(streams):
            result = Agent(
                model="fixture",
                provider=self.provider(),
                tools=[
                    Tool(
                        "echo",
                        "Echo",
                        {"type": "object"},
                        lambda arguments: arguments["text"],
                    )
                ],
            ).run("go")
        self.assertEqual(result.history[2]["output"], "café")
        self.assertEqual(
            json.loads(streams[1].request()[1]["input"][1]["arguments"]),
            {"text": "café"},
        )

    def test_cancel_and_timeout_release_headers_and_body(self):
        for headers in (False, True):
            for timeout in (False, True):
                stream = FixtureSocket(
                    b"" if headers else wire(b": waiting\n\n", complete=False),
                    hold=True,
                )
                token = CancellationToken()
                failures = []

                def run():
                    try:
                        Agent(
                            model="fixture",
                            provider=self.provider(timeout=0.15 if timeout else 3),
                        ).run("go", cancellation=token)
                    except Exception as error:
                        failures.append(error)

                with self.patch_connections([stream]):
                    worker = threading.Thread(target=run, daemon=True)
                    worker.start()
                    try:
                        self.assertTrue(stream.source.waiting.wait(1))
                        if not timeout:
                            token.cancel()
                        worker.join(1)
                        self.assertFalse(worker.is_alive())
                        self.assertEqual(len(failures), 1)
                        self.assertIsInstance(
                            failures[0], ProviderError if timeout else CancelledError
                        )
                        if timeout:
                            self.assertIn("timed out", str(failures[0]))
                        self.assertTrue(stream.closed)
                        self.assertTrue(stream.source.closed)
                    finally:
                        stream.source.stopped.set()
                        worker.join(1)

    def test_terminal_and_consumer_exit_close_unfinished_body(self):
        for anthropic in (False, True):
            for consumer_fails in (False, True):
                stream = FixtureSocket(
                    wire(fixture(anthropic), complete=False), hold=True
                )
                failure = RuntimeError("consumer stopped")

                def consume(event):
                    if consumer_fails and event.type == "text_delta":
                        raise failure

                with self.patch_connections([stream]):
                    agent = Agent(model="fixture", provider=self.provider(anthropic))
                    if consumer_fails:
                        with self.assertRaises(RuntimeError) as raised:
                            agent.run("go", on_event=consume)
                        self.assertIs(raised.exception, failure)
                    else:
                        self.assertEqual(
                            agent.run("go", on_event=consume).text, "done\ncafé"
                        )
                self.assertTrue(stream.closed)
                self.assertTrue(stream.source.closed)
                self.assertTrue(stream.stopped)
                self.assertFalse(stream.source.waiting.is_set())

    def test_credentials_are_resolved_at_request_time(self):
        for provider_class, environment in (
            (OpenAIProvider, "OPENAI_API_KEY"),
            (AnthropicProvider, "ANTHROPIC_API_KEY"),
        ):
            with patch.dict(os.environ, {}, clear=True):
                provider = provider_class()
                with self.assertRaisesRegex(ProviderError, environment):
                    Agent(model="fixture", provider=provider).run("go")
                token = CancellationToken()
                token.cancel()
                with self.assertRaises(CancelledError):
                    Agent(model="fixture", provider=provider).run(
                        "go", cancellation=token
                    )
                provider.endpoint = "http://fixture.invalid/events"
                os.environ[environment] = "late-key"
                anthropic = provider_class is AnthropicProvider
                stream = FixtureSocket(wire(fixture(anthropic)))
                with self.patch_connections([stream]):
                    self.assertEqual(
                        Agent(model="fixture", provider=provider).run("go").text,
                        "done\ncafé",
                    )
                self.assertIn("late-key", stream.request()[0])

    def test_error_response_is_bounded_and_closed(self):
        stream = FixtureSocket(wire(b"denied" * 10000, status=401), chunk_size=8192)
        with self.patch_connections([stream]):
            with self.assertRaises(ProviderError) as raised:
                Agent(model="fixture", provider=self.provider()).run("go")
        self.assertIn("API error 401", str(raised.exception))
        self.assertLess(len(str(raised.exception)), 8300)
        self.assertTrue(stream.closed)
        self.assertTrue(stream.source.closed)


class ProviderTcpTests(unittest.TestCase):
    def check_provider(self, anthropic):
        release = threading.Event()
        body_started = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                data = (
                    fixture(anthropic) if self.path == "/complete" else b": waiting\n\n"
                )
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
                if self.path == "/blocked":
                    body_started.set()
                release.wait(3)

        # Binding is mandatory. Restricted environments report failure here.
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        provider_class = AnthropicProvider if anthropic else OpenAIProvider
        try:
            result = Agent(
                model="fixture",
                provider=provider_class(
                    api_key="fixture-key",
                    endpoint=base + "/complete",
                    timeout=2,
                ),
            ).run("go")
            self.assertEqual(result.text, "done\ncafé")
            token = CancellationToken()
            failures = []

            def blocked():
                try:
                    Agent(
                        model="fixture",
                        provider=provider_class(
                            api_key="fixture-key",
                            endpoint=base + "/blocked",
                            timeout=2,
                        ),
                    ).run("go", cancellation=token)
                except Exception as error:
                    failures.append(error)

            worker = threading.Thread(target=blocked, daemon=True)
            worker.start()
            try:
                self.assertTrue(body_started.wait(1))
                token.cancel()
                worker.join(1)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(failures), 1)
                self.assertIsInstance(failures[0], CancelledError)
            finally:
                token.cancel()
                worker.join(3)
        finally:
            release.set()
            server.shutdown()
            server_thread.join(2)
            server.server_close()

    def test_openai_real_tcp_completion_and_cancellation(self):
        self.check_provider(False)

    def test_anthropic_real_tcp_completion_and_cancellation(self):
        self.check_provider(True)


if __name__ == "__main__":
    unittest.main()
