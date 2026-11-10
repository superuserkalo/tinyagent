"""Run cleanup and replay invariants shared by the native SDKs."""

import unittest
from contextlib import closing

from tinyagent import (
    Agent,
    AnthropicProvider,
    CancellationToken,
    CancelledError,
    MAX_TOOL_OUTPUT_BYTES,
    OpenAIProvider,
    ProviderError,
    ProviderEvent,
    ProviderTurn,
    Tool,
    ToolCall,
)


def turn(text):
    return ProviderTurn(
        text,
        {"role": "assistant", "content": [{"type": "text", "text": text}]},
        [],
        [],
    )


class LifecycleTests(unittest.TestCase):
    def test_terminal_cancellation_and_consumer_failure_prevent_a_result(self):
        for interface in ("run", "stream"):
            for failure in ("cancel", "consumer"):
                with self.subTest(interface=interface, failure=failure):
                    closed = []

                    class Provider:
                        def stream(self, model, instructions, history, tools, cancellation):
                            try:
                                yield ProviderEvent("text_delta", "done")
                                return turn("done")
                            finally:
                                closed.append(True)

                    agent = Agent(model="test", provider=Provider())
                    token = CancellationToken()
                    history = [{"role": "user", "content": "earlier"}]
                    events = []
                    consumer_error = RuntimeError("terminal delivery failed")
                    expected = CancelledError if failure == "cancel" else RuntimeError

                    def consume(event):
                        events.append(event)
                        if event.type == "agent_end":
                            self.assertEqual(closed, [True])
                            if failure == "cancel":
                                token.cancel()
                            else:
                                raise consumer_error

                    with self.assertRaises(expected) as raised:
                        if interface == "run":
                            agent.run("go", history, cancellation=token, on_event=consume)
                        else:
                            stream = agent.stream("go", history, cancellation=token)
                            try:
                                for event in stream:
                                    events.append(event)
                                    if event.type == "agent_end":
                                        self.assertEqual(closed, [True])
                                        if failure == "cancel":
                                            token.cancel()
                                            next(stream)
                                        else:
                                            stream.throw(consumer_error)
                                            self.fail("stream yielded another terminal event")
                            finally:
                                stream.close()
                    if failure == "consumer":
                        self.assertIs(raised.exception, consumer_error)
                    self.assertEqual(
                        [event.type for event in events],
                        ["agent_start", "text_delta", "agent_end"],
                    )
                    self.assertEqual([event.sequence for event in events], [1, 2, 3])
                    self.assertEqual(len({event.run_id for event in events}), 1)
                    self.assertEqual(history, [{"role": "user", "content": "earlier"}])
                    self.assertEqual(agent.run("again", history).text, "done")
                    self.assertEqual(closed, [True, True])

    def test_cancellation_wins_when_provider_returns_normally(self):
        cancellation = CancellationToken()
        emitted = []

        class Provider:
            def stream(self, model, instructions, history, tools, token):
                cancellation.cancel()
                yield from ()
                return turn("too late")

        with self.assertRaises(CancelledError):
            Agent(model="test", provider=Provider()).run(
                "go",
                cancellation=cancellation,
                on_event=emitted.append,
            )
        self.assertEqual(
            [event.type for event in emitted], ["agent_start", "agent_end"]
        )
        self.assertEqual(emitted[-1].data["status"], "aborted")

    def test_early_stream_exit_cancels_and_closes_provider(self):
        closed = []
        parent = CancellationToken()

        class Provider:
            def stream(self, model, instructions, history, tools, token):
                try:
                    yield ProviderEvent("text_delta", "one")
                    return turn("done")
                finally:
                    closed.append(token.cancelled)

        with closing(
            Agent(model="test", provider=Provider()).stream("go", cancellation=parent)
        ) as stream:
            for event in stream:
                if event.type == "text_delta":
                    break
        self.assertEqual(closed, [True])
        self.assertFalse(parent.cancelled)

    def test_failing_consumer_closes_provider(self):
        closed = []

        class Provider:
            def stream(self, model, instructions, history, tools, token):
                try:
                    yield ProviderEvent("text_delta", "one")
                    return turn("done")
                finally:
                    closed.append(token.cancelled)

        def consume(event):
            if event.type == "text_delta":
                raise RuntimeError("writer closed")

        with self.assertRaisesRegex(RuntimeError, "writer closed"):
            Agent(model="test", provider=Provider()).run("go", on_event=consume)
        self.assertEqual(closed, [True])

    def test_provider_cannot_mutate_prior_history(self):
        history = [{"role": "user", "content": "keep"}]

        class Provider:
            def stream(self, model, instructions, history, tools, token):
                yield from ()
                history[0]["content"] = "mutated"
                raise ProviderError("failed")

        with self.assertRaisesRegex(ProviderError, "failed"):
            Agent(model="test", provider=Provider()).run("go", history)
        self.assertEqual(history, [{"role": "user", "content": "keep"}])

    def test_oversized_tool_exception_is_bounded_and_recoverable(self):
        def fail(arguments):
            raise RuntimeError("🔥" * MAX_TOOL_OUTPUT_BYTES)

        observed = []

        class Provider:
            calls = 0

            def stream(self, model, instructions, history, tools, token):
                self.calls += 1
                yield from ()
                if self.calls == 1:
                    return ProviderTurn(
                        "",
                        {"role": "assistant", "content": []},
                        [],
                        [ToolCall("c", "fail", {})],
                    )
                observed.append(history[-1])
                return turn("recovered")

        tool = Tool("fail", "Fail", {"type": "object"}, fail)
        result = Agent(model="test", provider=Provider(), tools=[tool]).run("go")
        self.assertEqual(result.text, "recovered")
        self.assertTrue(observed[0]["isError"])
        self.assertLess(len(observed[0]["output"].encode()), 200)

    def test_provider_terminal_events_close_transport_without_reading_further(self):
        for provider_class, messages in (
            (
                OpenAIProvider,
                [{"type": "response.completed", "response": {"output": []}}],
            ),
            (
                AnthropicProvider,
                [
                    {"type": "message_start", "message": {"id": "m", "content": []}},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                    {"type": "message_stop"},
                ],
            ),
        ):
            with self.subTest(provider=provider_class.__name__):
                closed = []

                def transport(payload, cancellation):
                    try:
                        yield from messages
                        raise AssertionError("read beyond terminal event")
                    finally:
                        closed.append(True)

                Agent(
                    model="test", provider=provider_class(event_transport=transport)
                ).run("go")
                self.assertEqual(closed, [True])


if __name__ == "__main__":
    unittest.main()
