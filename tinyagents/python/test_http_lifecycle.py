"""HTTP parsing and response lifecycle through the real request handler."""

import http.client
import json
import threading
import unittest

from tinyagent import (
    Agent,
    MAX_JSONL_LINE_BYTES,
    ProviderError,
    ProviderEvent,
    ProviderTurn,
    Tool,
    ToolCall,
)
from http_fixture import HttpFixture, until


def turn(text="done"):
    return ProviderTurn(
        text, {"role": "assistant", "content": [{"type": "text", "text": text}]}, [], []
    )


class HttpLifecycleTests(unittest.TestCase):
    def start(self, provider=None, token=None):
        agent = (
            provider
            if isinstance(provider, Agent)
            else (
                Agent(model="test", provider=provider)
                if provider
                else Agent(model="test", transport=lambda payload: [])
            )
        )
        fixture = HttpFixture(agent, token)
        self.addCleanup(fixture.close)
        return fixture

    def test_native_http_framing_and_independent_histories(self):
        histories = []

        class Provider:
            def stream(self, model, instructions, history, tools, token):
                histories.append(len(history))
                yield from ()
                return turn()

        fixture = self.start(Provider())
        one, two = fixture.session(), fixture.session()
        for session in (one, one, two):
            response = fixture.prompt(session, "go")
            self.assertEqual(response.status, 200)
            events = [json.loads(line) for line in response.read().splitlines()]
            self.assertEqual(
                [event["type"] for event in events], ["agent_start", "agent_end"]
            )
            self.assertEqual([event["sequence"] for event in events], [1, 2])
            self.assertEqual(events[-1]["status"], "completed")
            fixture.wait_idle(session)
        self.assertEqual(histories, [1, 3, 1])

    def test_duplicate_authorization_is_rejected(self):
        fixture = self.start(token="secret")
        response = fixture.call(
            "GET",
            "/health",
            headers=[
                ("Authorization", "Bearer secret"),
                ("Authorization", "Bearer wrong"),
            ],
        )
        self.assertEqual(response.status, 401)
        self.assertEqual(json.loads(response.read())["error"]["code"], "unauthorized")

    def test_cancel_rechecks_session_after_its_body_finishes(self):
        fixture = self.start()
        session = fixture.session()
        pending = fixture.headers(
            "POST",
            f"/sessions/{session}/cancel",
            [("Content-Length", "2"), ("Content-Type", "application/json")],
        )
        pending.send(b"{")
        until(pending.server_socket.waiting_for_body)
        deleted = fixture.call("DELETE", f"/sessions/{session}")
        self.assertEqual(deleted.status, 204)
        deleted.read()
        pending.send(b"}")
        response = pending.getresponse()
        self.assertEqual(response.status, 404)
        response.read()

    def test_terminal_framing_releases_busy_state_and_preserves_committed_history(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                histories = []

                class Provider:
                    def stream(self, model, instructions, history, tools, token):
                        histories.append(len(history))
                        yield from ()
                        return turn()

                fixture = self.start(Provider())
                session = fixture.session()
                fixture.prompt(session, "first").read()
                fixture.wait_idle(session)
                entered, release = threading.Event(), threading.Event()

                def hold(data):
                    if data == b"0\r\n\r\n":
                        entered.set()
                        if not release.wait(3):
                            raise AssertionError("terminal framing was not released")
                        if fail:
                            raise BrokenPipeError("terminal framing failed")

                response = fixture.prompt(session, "second", hold)
                try:
                    self.assertTrue(response.chunked)
                    self.assertTrue(entered.wait(1))
                    following = fixture.prompt(session, "next")
                    self.assertEqual(following.status, 200)
                    following.read()
                finally:
                    release.set()
                if fail:
                    with self.assertRaises(http.client.IncompleteRead):
                        response.read()
                else:
                    response.read()
                fixture.wait_idle(session)
                self.assertEqual(histories, [1, 3, 5])

    def test_cancel_delete_and_shutdown_release_stalled_output(self):
        for action in ("cancel", "delete", "shutdown"):
            with self.subTest(action=action):
                closed = threading.Event()
                produced = []

                class Provider:
                    def stream(self, model, instructions, history, tools, token):
                        if history[-1]["content"] == "keep":
                            return turn()
                        try:
                            for _ in range(100):
                                produced.append(True)
                                yield ProviderEvent("text_delta", "x" * 100_000)
                            return turn()
                        finally:
                            closed.set()

                fixture = self.start(Provider())
                session_id = fixture.session()
                fixture.prompt(session_id, "keep").read()
                fixture.wait_idle(session_id)
                pending = fixture.prompt(session_id, "flood")
                self.assertEqual(pending.status, 200)
                connection = fixture.clients[-1].server_socket
                until(connection.waiting_for_output)
                session = fixture.application._sessions[session_id]
                run = session.active
                if action == "cancel":
                    response = fixture.call("POST", f"/sessions/{session_id}/cancel")
                    self.assertTrue(json.loads(response.read())["accepted"])
                elif action == "delete":
                    response = fixture.call("DELETE", f"/sessions/{session_id}")
                    self.assertEqual(response.status, 204)
                    response.read()
                else:
                    fixture.application.close()
                self.assertTrue(run.done.wait(2))
                self.assertTrue(closed.is_set())
                self.assertEqual(len(produced), 1)
                self.assertEqual(len(session.history), 2)
                with self.assertRaises(http.client.IncompleteRead):
                    pending.read()
                pending.close()

    def test_delete_and_shutdown_wait_for_tool_cleanup_without_closing_good_output(
        self,
    ):
        started, cancelled, cleaning, release = (threading.Event() for _ in range(4))

        def wait(arguments, token):
            remove = token.on_cancel(cancelled.set)
            try:
                started.set()
                if not cancelled.wait(3):
                    raise AssertionError("tool was not cancelled")
                token.raise_if_cancelled()
            finally:
                remove()
                cleaning.set()
                if not release.wait(3):
                    raise AssertionError("tool cleanup was not released")

        class Provider:
            def stream(self, model, instructions, history, tools, token):
                yield from ()
                return ProviderTurn(
                    "",
                    {"role": "assistant", "content": []},
                    [],
                    [ToolCall("c", "wait", {})],
                )

        fixture = self.start(
            Agent(
                model="test",
                provider=Provider(),
                tools=[Tool("wait", "Wait", {}, wait)],
            )
        )
        session_id = fixture.session()
        pending = fixture.prompt(session_id, "wait")
        connection = fixture.clients[-1].server_socket
        self.assertTrue(started.wait(1))
        events = [json.loads(pending.readline()), json.loads(pending.readline())]
        session = fixture.application._sessions[session_id]
        run = session.active
        cancel = fixture.call("POST", f"/sessions/{session_id}/cancel")
        cancel.read()
        self.assertTrue(cleaning.wait(1))
        deletion = fixture.headers("DELETE", f"/sessions/{session_id}")
        until(lambda: session.deleted)
        closing = threading.Thread(target=fixture.application.close, daemon=True)
        closing.start()
        try:
            # Version 2 (G24): a cancelled run waits one second for host code, no longer.
            self.assertFalse(run.done.wait(0.5))
            self.assertTrue(closing.is_alive())
            self.assertIn(session_id, fixture.application._sessions)
            self.assertFalse(connection.outgoing.write_closed)
        finally:
            release.set()
            closing.join(3)
        self.assertFalse(closing.is_alive())
        self.assertTrue(run.done.is_set())
        events.extend(json.loads(line) for line in pending.read().splitlines())
        self.assertEqual(
            [event["type"] for event in events],
            ["agent_start", "tool_execution_start", "tool_execution_end", "agent_end"],
        )
        self.assertEqual(events[-1]["status"], "aborted")
        self.assertEqual(events[2]["toolCallId"], events[1]["toolCallId"])
        self.assertTrue(events[2]["isError"])

    def test_disconnect_cancels_tool_and_preserves_completed_history(self):
        started, stopped, cleaned = (threading.Event() for _ in range(3))
        histories = []

        def wait(arguments, token):
            remove = token.on_cancel(stopped.set)
            try:
                started.set()
                if not stopped.wait(3):
                    raise AssertionError("disconnect did not cancel the tool")
                token.raise_if_cancelled()
            finally:
                remove()
                cleaned.set()

        class Provider:
            def stream(self, model, instructions, history, tools, token):
                histories.append(len(history))
                yield from ()
                if history[-1]["content"] == "wait":
                    return ProviderTurn(
                        "",
                        {"role": "assistant", "content": []},
                        [],
                        [ToolCall("c", "wait", {})],
                    )
                return turn()

        fixture = self.start(
            Agent(
                model="test",
                provider=Provider(),
                tools=[Tool("wait", "Wait", {}, wait)],
            )
        )
        session_id = fixture.session()
        fixture.prompt(session_id, "keep").read()
        fixture.wait_idle(session_id)
        pending = fixture.prompt(session_id, "wait")
        self.assertTrue(started.wait(1))
        run = fixture.application._sessions[session_id].active
        pending.close()
        self.assertTrue(cleaned.wait(1))
        self.assertTrue(run.done.wait(1))
        # Version 2 (F6): a cancelled run never requires reconciliation. Its
        # unfinished tool is recorded as interrupted and the next prompt is accepted.
        core = fixture.application._sessions[session_id].core
        self.assertEqual(len(core.history), 2)
        self.assertFalse(core.recovery_required)
        self.assertEqual([record["type"] for record in core.records[-2:]], ["tool_result", "run_failed"])
        accepted = fixture.prompt(session_id, "retry")
        self.assertEqual(accepted.status, 200)
        accepted.read()
        self.assertEqual(histories, [1, 3, 3])

    def test_provider_failure_and_oversized_event_have_one_terminal_and_roll_back(self):
        for oversized in (False, True):
            with self.subTest(oversized=oversized):
                histories = []
                cleaned = threading.Event()

                class Provider:
                    def stream(self, model, instructions, history, tools, token):
                        histories.append((len(history), history[0]["content"]))
                        if history[-1]["content"] == "fail":
                            history[0]["content"] = "corrupt"
                            try:
                                yield ProviderEvent(
                                    "text_delta",
                                    (
                                        "x" * MAX_JSONL_LINE_BYTES
                                        if oversized
                                        else "partial"
                                    ),
                                )
                                raise ProviderError("fixture failure")
                            finally:
                                cleaned.set()
                        return turn()

                fixture = self.start(Provider())
                session_id = fixture.session()
                fixture.prompt(session_id, "keep").read()
                fixture.wait_idle(session_id)
                response = fixture.prompt(session_id, "fail")
                events = [json.loads(line) for line in response.read().splitlines()]
                self.assertEqual(
                    sum(event["type"] == "agent_end" for event in events), 1
                )
                self.assertEqual(events[-1]["status"], "failed")
                self.assertEqual(
                    events[-1]["error"]["code"],
                    "output_too_large" if oversized else "provider_error",
                )
                self.assertEqual(
                    [event["sequence"] for event in events],
                    list(range(1, len(events) + 1)),
                )
                self.assertTrue(cleaned.wait(1))
                fixture.wait_idle(session_id)
                fixture.prompt(session_id, "retry").read()
                self.assertEqual(histories, [(1, "keep"), (3, "keep"), (3, "keep")])


if __name__ == "__main__":
    unittest.main()
