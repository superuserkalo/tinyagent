"""A model turn with no content (Anthropic end_turn with an empty content
array; a chat completion that stops with no text) completes the run and is
committed. The next request replays it as an assistant message with empty or
null content, which both APIs reject, so every later prompt fails."""
import os, sys, unittest
from tinyagent import Agent, Session, AnthropicProvider, ChatCompletionsProvider


class EmptyAssistant(unittest.TestCase):
    def test_g03_anthropic_never_sends_empty_assistant_content(self):
        payloads = []
        def transport(payload, cancellation):
            payloads.append(payload)
            yield {"type": "message_start", "message": {"id": "m", "role": "assistant", "content": [],
                                                        "usage": {"input_tokens": 1, "output_tokens": 0}}}
            yield {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 0}}
            yield {"type": "message_stop"}
        session = Session(Agent(model="m", provider=AnthropicProvider("k", transport)))
        session.run("one")
        session.run("two")
        for message in payloads[-1]["messages"]:
            self.assertTrue(message["content"], f"empty content sent to Anthropic: {message}")

    def test_g03_chat_never_sends_null_assistant_content_without_tool_calls(self):
        payloads = []
        def transport(payload, cancellation):
            payloads.append(payload)
            yield {"choices": [{"delta": {"role": "assistant"}, "finish_reason": "stop"}]}
        session = Session(Agent(model="m", provider=ChatCompletionsProvider("k", transport)))
        session.run("one")
        session.run("two")
        for message in payloads[-1]["messages"]:
            if message["role"] == "assistant":
                self.assertTrue(message.get("content") is not None or message.get("tool_calls"),
                                f"assistant message with null content and no tool_calls: {message}")


if __name__ == "__main__":
    unittest.main()
