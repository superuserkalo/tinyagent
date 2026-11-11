"""Chat Completions: two tool calls in one turn, the first returns an image.
The image is sent as a user message placed between the two tool messages, so
the second tool message no longer follows the assistant tool_calls message."""
import os, sys, unittest
from tinyagent import Agent, Session, Tool, ChatCompletionsProvider


class ChatOrder(unittest.TestCase):
    def test_g15_tool_messages_directly_follow_their_assistant_message(self):
        payloads = []
        def transport(payload, cancellation):
            payloads.append(payload)
            if len(payloads) == 1:
                yield {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "a", "function": {"name": "shot", "arguments": "{}"}},
                    {"index": 1, "id": "b", "function": {"name": "note", "arguments": "{}"}}]},
                    "finish_reason": "tool_calls"}]}
            else:
                yield {"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}
        image = [{"type": "image", "mediaType": "image/png", "data": "AAAA"}]
        agent = Agent(model="m", provider=ChatCompletionsProvider("k", transport), tools=[
            Tool("shot", "shot", {"type": "object"}, lambda a: image),
            Tool("note", "note", {"type": "object"}, lambda a: "ok")])
        Session(agent).run("go")
        messages = payloads[1]["messages"]
        index = next(i for i, m in enumerate(messages) if m.get("tool_calls"))
        following = [m["role"] for m in messages[index + 1:index + 3]]
        self.assertEqual(following, ["tool", "tool"],
                         f"a non-tool message separates the tool results: {[m['role'] for m in messages]}")


if __name__ == "__main__":
    unittest.main()
