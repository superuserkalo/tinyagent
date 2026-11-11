"""RPC acknowledgement, cleanup, and completed-history ownership."""

import threading
import unittest

from tinyagent import (
    Agent,
    CancellationToken,
    ProviderEvent,
    ProviderTurn,
    RpcSession,
    Tool,
    ToolCall,
)


def completed(text="done"):
    return ProviderTurn(
        text,
        {"role": "assistant", "content": [{"type": "text", "text": text}]},
        [],
        [],
    )


class Task:
    """Capture a test thread's result and always join it before leaving a test."""

    def __init__(self, action):
        self.errors = []
        self.done = threading.Event()

        def run():
            try:
                action()
            except BaseException as error:
                self.errors.append(error)
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def join(self):
        self.thread.join(3)
        if self.thread.is_alive():
            raise AssertionError("RPC task did not finish")
        if self.errors:
            raise self.errors[0]


class RpcLifecycleTests(unittest.TestCase):
    def test_refused_resume_answers_and_leaves_the_connection_usable(self):
        emitted = []
        session = RpcSession(Agent(model="test", transport=lambda payload: []), emitted.append)
        self.addCleanup(session.close)
        refused = Task(lambda: session.handle({"id": "resume", "type": "resume_run"}))
        refused.join()
        self.assertEqual(emitted[-1]["error"]["code"], "nothing_to_resume")
        session.handle({"id": "state", "type": "get_state"})
        self.assertEqual(emitted[-1]["data"]["status"], "idle")

    def test_close_waits_for_prompt_acknowledgement_before_returning(self):
        for fail_ack in (False, True):
            with self.subTest(fail_ack=fail_ack):
                entered = threading.Event()
                release = threading.Event()
                provider_calls = []
                emitted = []

                def emit(value):
                    if value.get("command") == "prompt":
                        entered.set()
                        if not release.wait(3):
                            raise AssertionError("acknowledgement was not released")
                        if fail_ack:
                            raise BrokenPipeError("acknowledgement failed")
                    emitted.append(value)

                def transport(payload):
                    provider_calls.append(payload)
                    return []

                session = RpcSession(Agent(model="test", transport=transport), emit)
                prompt = Task(
                    lambda: session.handle({"type": "prompt", "message": "go"})
                )
                self.assertTrue(entered.wait(1))
                close = Task(session.close)
                try:
                    self.assertFalse(close.done.wait(0.05))
                    self.assertFalse(session.wait_idle(0.01))
                    self.assertEqual(emitted, [])
                finally:
                    release.set()
                    if fail_ack:
                        with self.assertRaisesRegex(
                            BrokenPipeError, "acknowledgement failed"
                        ):
                            prompt.join()
                    else:
                        prompt.join()
                    close.join()
                self.assertTrue(session.wait_idle(0))
                self.assertEqual(provider_calls, [])

    def test_reset_waits_for_acknowledgement_and_keeps_run_busy(self):
        entered = threading.Event()
        release = threading.Event()
        emitted = []

        def emit(value):
            if value.get("id") == "prompt":
                entered.set()
                if not release.wait(3):
                    raise AssertionError("acknowledgement was not released")
            emitted.append(value)

        session = RpcSession(Agent(model="test", transport=lambda payload: []), emit)
        prompt = Task(
            lambda: session.handle({"id": "prompt", "type": "prompt", "message": "go"})
        )
        self.assertTrue(entered.wait(1))
        reset = Task(lambda: session.handle({"id": "reset", "type": "new_session"}))
        try:
            self.assertFalse(reset.done.wait(0.05))
            session.handle({"id": "busy", "type": "prompt", "message": "again"})
            self.assertEqual(emitted[-1]["error"]["code"], "busy")
        finally:
            release.set()
            prompt.join()
            reset.join()
            session.close()
        session.handle({"type": "get_state"})
        self.assertEqual(emitted[-1]["data"]["messageCount"], 0)
        self.assertTrue(
            next(item for item in emitted if item.get("id") == "reset")["data"][
                "abortedRun"
            ]
        )

    def test_failed_acknowledgement_releases_run_without_calling_provider(self):
        calls = []
        emitted = []

        def emit(value):
            if value.get("id") == "fail":
                raise BrokenPipeError("failed")
            emitted.append(value)

        def transport(payload):
            calls.append(payload)
            return []

        session = RpcSession(Agent(model="test", transport=transport), emit)
        with self.assertRaises(BrokenPipeError):
            session.handle({"id": "fail", "type": "prompt", "message": "go"})
        self.assertTrue(session.wait_idle(0))
        self.assertEqual(calls, [])
        session.handle({"id": "ok", "type": "prompt", "message": "retry"})
        self.assertTrue(session.wait_idle(1))
        self.assertEqual(len(calls), 1)
        self.assertEqual(emitted[0]["id"], "ok")

    def test_history_commits_before_terminal_delivery(self):
        for fail_terminal in (False, True):
            with self.subTest(fail_terminal=fail_terminal):
                entered = threading.Event()
                release = threading.Event()
                emitted = []
                histories = []

                class Provider:
                    def stream(self, model, instructions, history, tools, token):
                        histories.append(len(history))
                        yield from ()
                        return completed()

                def emit(value):
                    if value["type"] == "agent_end" and len(histories) == 2:
                        entered.set()
                        if not release.wait(3):
                            raise AssertionError("terminal write was not released")
                        if fail_terminal:
                            raise BrokenPipeError("terminal delivery failed")
                    emitted.append(value)

                session = RpcSession(Agent(model="test", provider=Provider()), emit)
                # close() ends the RPC session, so it runs after the last prompt.
                self.addCleanup(session.close)
                session.handle({"type": "prompt", "message": "first"})
                self.assertTrue(session.wait_idle(1))
                session.handle({"type": "prompt", "message": "second"})
                try:
                    self.assertTrue(entered.wait(1))
                    session.handle({"type": "get_state"})
                    self.assertEqual(emitted[-1]["data"]["messageCount"], 4)
                    self.assertEqual(emitted[-1]["data"]["status"], "idle")
                    session.handle({"id": "next", "type": "prompt", "message": "next"})
                    self.assertTrue(session.wait_idle(1))
                finally:
                    release.set()
                    self.assertTrue(session.wait_idle(1))
                self.assertEqual(histories, [1, 3, 5])

    def test_reset_waits_for_tool_cleanup_and_preserves_event_order(self):
        entered = threading.Event()
        cancelled = threading.Event()
        release = threading.Event()
        emitted = []

        class Provider:
            def stream(self, model, instructions, history, tools, token):
                yield from ()
                return ProviderTurn(
                    "",
                    {"role": "assistant", "content": []},
                    [],
                    [ToolCall("c", "wait", {})],
                )

        def tool(arguments, token):
            remove = token.on_cancel(cancelled.set)
            entered.set()
            try:
                if not cancelled.wait(3) or not release.wait(3):
                    raise AssertionError("tool cleanup did not finish")
                token.raise_if_cancelled()
            finally:
                remove()

        session = RpcSession(
            Agent(
                model="test",
                provider=Provider(),
                tools=[Tool("wait", "Wait", {"type": "object"}, tool)],
            ),
            emitted.append,
        )
        session.handle({"type": "prompt", "message": "go"})
        self.assertTrue(entered.wait(1))
        reset = Task(lambda: session.handle({"id": "reset", "type": "new_session"}))
        try:
            self.assertTrue(cancelled.wait(1))
            self.assertFalse(reset.done.wait(0.05))
        finally:
            release.set()
            reset.join()
            session.close()
        events = [item for item in emitted if "sequence" in item]
        self.assertEqual(
            [item["type"] for item in events],
            ["agent_start", "tool_execution_start", "tool_execution_end", "agent_end"],
        )
        self.assertEqual([item["sequence"] for item in events], [1, 2, 3, 4])
        self.assertEqual(len({item["runId"] for item in events}), 1)
        self.assertEqual(events[-1]["status"], "aborted")
        self.assertEqual(emitted[-1]["id"], "reset")

    def test_emitter_can_cancel_its_own_run_without_waiting_on_itself(self):
        for cancel_at in ("response", "text_delta"):
            with self.subTest(cancel_at=cancel_at):
                emitted = []
                provider_closed = []

                class Provider:
                    def stream(self, model, instructions, history, tools, token):
                        try:
                            yield ProviderEvent("text_delta", "one")
                            return completed()
                        finally:
                            provider_closed.append(token.cancelled)

                def emit(value):
                    emitted.append(value)
                    if (
                        value["type"] == cancel_at
                        and value.get("command") != "get_state"
                    ):
                        self.assertFalse(session.wait_idle(0.01))
                        session.close()

                session = RpcSession(Agent(model="test", provider=Provider()), emit)
                session.handle({"type": "prompt", "message": "go"})
                self.assertTrue(session.wait_idle(1))
                self.assertEqual(
                    provider_closed, [] if cancel_at == "response" else [True]
                )
                self.assertEqual(emitted[-1]["status"], "aborted")
                session.handle({"type": "get_state"})
                self.assertEqual(emitted[-1]["data"]["messageCount"], 0)

    def test_abort_after_commit_before_terminal_delivery_is_not_accepted(self):
        entered = threading.Event()
        release = threading.Event()
        histories = []
        emitted = []

        class Provider:
            def stream(self, model, instructions, history, tools, token):
                histories.append(len(history))
                yield from ()
                return completed()

        def emit(value):
            if value["type"] == "agent_end" and len(histories) == 2:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("terminal write was not released")
            emitted.append(value)

        session = RpcSession(Agent(model="test", provider=Provider()), emit)
        self.addCleanup(session.close)
        session.handle({"type": "prompt", "message": "first"})
        self.assertTrue(session.wait_idle(1))
        session.handle({"type": "prompt", "message": "second"})
        try:
            self.assertTrue(entered.wait(1))
            session.handle({"id": "abort", "type": "abort"})
            self.assertFalse(emitted[-1]["data"]["accepted"])
            self.assertTrue(session.wait_idle(0.01))
        finally:
            release.set()
            self.assertTrue(session.wait_idle(1))
        session.handle({"type": "prompt", "message": "third"})
        self.assertTrue(session.wait_idle(1))
        self.assertEqual(histories, [1, 3, 5])

    def test_closed_session_rejects_prompts_without_calling_provider(self):
        calls = []
        emitted = []

        def transport(payload):
            calls.append(payload)
            return []

        session = RpcSession(Agent(model="test", transport=transport), emitted.append)
        session.close()
        session.close()
        for command in ("prompt", "compact"):
            session.handle({"id": command, "type": command, "message": "go"})
            self.assertEqual(emitted[-1]["id"], command)
            self.assertEqual(emitted[-1]["error"]["code"], "session_closed")
        closed = session.session
        for command in ("new_session", "resume_session"):
            session.handle({"id": command, "type": command, "sessionId": "s_other"})
            self.assertEqual(emitted[-1]["error"]["code"], "session_closed")
        self.assertIs(session.session, closed)
        self.assertTrue(session.wait_idle(0))
        self.assertEqual(calls, [])

    def test_cancellable_emitter_releases_acknowledgement_on_close_or_parent_cancel(
        self,
    ):
        for cancel_parent in (False, True):
            with self.subTest(cancel_parent=cancel_parent):
                parent = CancellationToken()
                entered = threading.Event()
                cancelled = threading.Event()
                emitted = []
                tokens = []
                calls = []

                def emit(value, token):
                    if value.get("command") == "prompt":
                        remove = token.on_cancel(cancelled.set)
                        entered.set()
                        try:
                            self.assertTrue(cancelled.wait(2))
                        finally:
                            remove()
                    emitted.append(value)
                    if token:
                        tokens.append(token)

                def transport(payload):
                    calls.append(payload)
                    return []

                session = RpcSession(
                    Agent(model="test", transport=transport), emit, cancellation=parent
                )
                prompt = Task(
                    lambda: session.handle({"type": "prompt", "message": "go"})
                )
                try:
                    self.assertTrue(entered.wait(1))
                    if cancel_parent:
                        parent.cancel()
                    else:
                        session.close()
                finally:
                    parent.cancel()
                    prompt.join()
                    session.close()
                self.assertTrue(session.wait_idle(0))
                self.assertEqual(calls, [])
                self.assertEqual(len(tokens), 3)
                self.assertTrue(
                    all(token is tokens[0] and token.cancelled for token in tokens)
                )
                session.handle({"type": "get_state"})
                self.assertEqual(emitted[-1]["data"]["messageCount"], 0)


if __name__ == "__main__":
    unittest.main()
