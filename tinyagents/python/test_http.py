"""HTTP acceptance tests. These require permission to bind local TCP sockets."""

import http.client
import json
import socket
import threading
import time
import unittest
from urllib.parse import urlsplit

from tinyagent import (
    Agent,
    MAX_HTTP_BODY_BYTES,
    MAX_JSONL_LINE_BYTES,
    ProviderError,
    ProviderEvent,
    ProviderTurn,
    Tool,
    ToolCall,
    serve,
)


def turn(text):
    return ProviderTurn(
        text, {"role": "assistant", "content": [{"type": "text", "text": text}]}, [], []
    )


class HttpTests(unittest.TestCase):
    def start(self, provider, **options):
        agent = (
            provider
            if isinstance(provider, Agent)
            else Agent(model="test", provider=provider)
        )
        server = serve(agent, port=0, **options)
        self.addCleanup(server.close)
        return server

    def call(self, server, method, path, body=None, token=None):
        address = urlsplit(server.url)
        connection = http.client.HTTPConnection(
            address.hostname, address.port, timeout=3
        )
        self.addCleanup(connection.close)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body)
        connection.request(method, path, body, headers)
        return connection, connection.getresponse()

    def session(self, server, token=None):
        _, response = self.call(server, "POST", "/sessions", token=token)
        self.assertEqual(response.status, 201)
        session_id = json.loads(response.read())["sessionId"]
        self.assertRegex(session_id, r"^s_[0-9a-f]{32}$")
        return session_id

    def prompt(self, server, session_id, message, *, retry_busy=False):
        deadline = time.monotonic() + 3
        while True:
            connection, response = self.call(
                server, "POST", f"/sessions/{session_id}/prompts", {"message": message}
            )
            if not retry_busy or response.status != 409:
                return connection, response
            self.assertEqual(
                json.loads(response.read())["error"]["code"], "session_busy"
            )
            connection.close()
            self.assertLess(time.monotonic(), deadline, "Completed session stayed busy")
            time.sleep(0.01)

    def events(self, response):
        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.getheader("Content-Type"), "application/jsonl; charset=utf-8"
        )
        raw = response.read()
        self.assertTrue(raw.endswith(b"\n"))
        values = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual(values[0]["type"], "agent_start")
        self.assertEqual(values[-1]["type"], "agent_end")
        self.assertEqual(sum(event["type"] == "agent_end" for event in values), 1)
        self.assertEqual(
            [event["sequence"] for event in values], list(range(1, len(values) + 1))
        )
        self.assertEqual(len({event["runId"] for event in values}), 1)
        return values

    def test_authentication_loopback_and_routes(self):
        server = self.start(
            Agent(model="test", transport=lambda payload: []), token="secret"
        )
        self.assertTrue(server.url.startswith("http://127.0.0.1:"))
        for token in (None, "wrong"):
            _, response = self.call(server, "GET", "/health", token=token)
            self.assertEqual(response.status, 401)
            self.assertEqual(
                json.loads(response.read())["error"]["code"], "unauthorized"
            )
            self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        _, response = self.call(server, "GET", "/health", token="secret")
        self.assertEqual(
            json.loads(response.read()), {"status": "ok", "protocolVersion": 1}
        )
        self.session(server, token="secret")
        _, response = self.call(server, "GET", "/missing", token="secret")
        self.assertEqual(response.status, 404)
        response.read()

    def test_streams_and_independent_histories(self):
        histories = []

        class Provider:
            def stream(self, model, instructions, history, tools, cancellation):
                histories.append(history)
                yield ProviderEvent("text_delta", "hello\n")
                yield ProviderEvent("text_delta", "world")
                return turn("hello\nworld")

        server = self.start(Provider())
        one, two = self.session(server), self.session(server)
        for session_id, message in ((one, "first"), (one, "second"), (two, "other")):
            # Receiving the final bytes can precede the handler's history commit.
            _, response = self.prompt(server, session_id, message, retry_busy=True)
            events = self.events(response)
            self.assertEqual(events[-1]["text"], "hello\nworld")
        # Providers receive the mutable current transcript, which gains their completed answer.
        self.assertEqual([len(history) for history in histories], [2, 4, 2])

    def test_failure_rolls_back_history(self):
        observed = []

        class Provider:
            calls = 0

            def stream(self, model, instructions, history, tools, cancellation):
                self.calls += 1
                if self.calls == 2:
                    history[0]["content"] = "corrupted"
                    yield ProviderEvent("text_delta", "partial")
                    raise ProviderError("failed")
                observed.append((len(history), history[0]["content"]))
                return turn("done")

        server = self.start(Provider())
        session_id = self.session(server)
        statuses = []
        for message in ("keep", "discard", "retry"):
            _, response = self.prompt(server, session_id, message, retry_busy=True)
            statuses.append(self.events(response)[-1]["status"])
        self.assertEqual(statuses, ["completed", "failed", "completed"])
        self.assertEqual(observed, [(1, "keep"), (3, "keep")])

    def test_busy_concurrency_cancellation_and_deletion(self):
        started = threading.Event()

        class Provider:
            def stream(self, model, instructions, history, tools, cancellation):
                if history[-1]["content"] == "wait":
                    stopped = threading.Event()
                    remove = cancellation.on_cancel(stopped.set)
                    try:
                        yield ProviderEvent("text_delta", "waiting")
                        started.set()
                        stopped.wait(3)
                    finally:
                        remove()
                return turn("done")

        server = self.start(Provider())
        one, two = self.session(server), self.session(server)
        _, pending = self.prompt(server, one, "wait")
        self.assertTrue(started.wait(2))
        _, busy = self.prompt(server, one, "busy")
        self.assertEqual(busy.status, 409)
        self.assertEqual(json.loads(busy.read())["error"]["code"], "session_busy")
        _, other = self.prompt(server, two, "go")
        self.assertEqual(self.events(other)[-1]["status"], "completed")
        _, cancelled = self.call(server, "POST", f"/sessions/{one}/cancel")
        cancellation = json.loads(cancelled.read())
        self.assertTrue(cancellation["accepted"])
        aborted = self.events(pending)
        self.assertEqual(aborted[-1]["status"], "aborted")
        self.assertEqual(aborted[0]["runId"], cancellation["runId"])
        deadline = time.monotonic() + 3
        while True:
            _, idle = self.call(server, "POST", f"/sessions/{one}/cancel")
            state = json.loads(idle.read())
            if not state["accepted"]:
                self.assertEqual(state, {"accepted": False, "runId": None})
                break
            self.assertEqual(state, cancellation)
            self.assertLess(time.monotonic(), deadline, "Cancelled session stayed busy")
            time.sleep(0.01)
        _, deleted = self.call(server, "DELETE", f"/sessions/{one}")
        self.assertEqual(deleted.status, 204)
        deleted.read()
        _, missing = self.prompt(server, one, "missing")
        self.assertEqual(missing.status, 404)
        missing.read()

    def test_disconnect_cancels_active_tool(self):
        started, stopped = threading.Event(), threading.Event()

        def wait(arguments, cancellation):
            remove = cancellation.on_cancel(stopped.set)
            try:
                started.set()
                stopped.wait(3)
                cancellation.raise_if_cancelled()
            finally:
                remove()
            return "done"

        class Provider:
            def stream(self, model, instructions, history, tools, cancellation):
                yield from ()
                return ProviderTurn(
                    "",
                    {"role": "assistant", "content": []},
                    [],
                    [ToolCall("c", "wait", {})],
                )

        server = self.start(
            Agent(
                model="test",
                provider=Provider(),
                tools=[Tool("wait", "Wait", {}, wait)],
            )
        )
        session_id = self.session(server)
        address = urlsplit(server.url)
        connection = socket.create_connection(
            (address.hostname, address.port), timeout=3
        )
        self.addCleanup(connection.close)
        body = b'{"message":"wait"}'
        connection.sendall(
            (
                f"POST /sessions/{session_id}/prompts HTTP/1.1\r\n"
                f"Host: {address.netloc}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
            ).encode()
            + body
        )
        self.assertTrue(started.wait(2))
        connection.shutdown(socket.SHUT_RDWR)
        connection.close()
        self.assertTrue(stopped.wait(2))

    def test_delete_and_shutdown_wait_for_provider_cleanup(self):
        cleaned = []

        class Provider:
            def stream(self, model, instructions, history, tools, cancellation):
                stopped = threading.Event()
                remove = cancellation.on_cancel(stopped.set)
                try:
                    yield ProviderEvent("text_delta", "waiting")
                    stopped.wait(3)
                    return turn("cancelled")
                finally:
                    remove()
                    cleaned.append(True)

        server = self.start(Provider())
        one, two = self.session(server), self.session(server)
        _, first = self.prompt(server, one, "wait")
        _, second = self.prompt(server, two, "wait")
        _, deleted = self.call(server, "DELETE", f"/sessions/{one}")
        self.assertEqual(deleted.status, 204)
        deleted.read()
        self.assertEqual(self.events(first)[-1]["status"], "aborted")
        self.assertEqual(len(cleaned), 1)
        server.close()
        self.assertEqual(len(cleaned), 2)
        second.close()
        server.close()

    def test_validation_and_limits(self):
        server = self.start(Agent(model="test", transport=lambda payload: []))
        session_id = self.session(server)
        for body in ({}, {"message": ""}):
            _, response = self.call(
                server, "POST", f"/sessions/{session_id}/prompts", body
            )
            self.assertEqual(response.status, 400)
            self.assertIn("error", json.loads(response.read()))

        # A known oversized body is rejected from its headers, before any upload.
        address = urlsplit(server.url)
        connection = http.client.HTTPConnection(
            address.hostname, address.port, timeout=3
        )
        self.addCleanup(connection.close)
        connection.putrequest("POST", f"/sessions/{session_id}/prompts")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(MAX_HTTP_BODY_BYTES + 1))
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        self.assertEqual(
            json.loads(response.read())["error"]["code"], "request_too_large"
        )

    def test_oversized_events_close_provider(self):
        closed = threading.Event()

        class Provider:
            def stream(self, model, instructions, history, tools, cancellation):
                try:
                    yield ProviderEvent("text_delta", "x" * MAX_JSONL_LINE_BYTES)
                    return turn("large")
                finally:
                    closed.set()

        server = self.start(Provider())
        _, response = self.prompt(server, self.session(server), "go")
        events = self.events(response)
        self.assertEqual(events[-1]["error"]["code"], "output_too_large")
        self.assertTrue(closed.wait(2))


class ExposureTests(unittest.TestCase):
    def test_remote_binding_requires_token_and_opt_in(self):
        agent = Agent(model="test", transport=lambda payload: [])
        for options in ({}, {"token": "secret"}, {"allow_remote": True}):
            with self.subTest(options=options), self.assertRaisesRegex(
                ValueError, "remote binding"
            ):
                serve(agent, host="0.0.0.0", port=0, **options)
        with self.assertRaisesRegex(ValueError, "token"):
            serve(agent, port=0, token="")


if __name__ == "__main__":
    unittest.main()
