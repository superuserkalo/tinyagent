"""ToolCall.arguments is the same dict as the assistant block in the live
transcript. A handler (or before_tool) that edits its arguments in place
rewrites what the model is told it asked for; the journal keeps the original,
so the request sent mid-run and the journal disagree."""
import json, os, sys, unittest
from tinyagent import Agent, Session, Tool, ChatCompletionsProvider


class Mutation(unittest.TestCase):
    def test_g09_handler_cannot_rewrite_the_models_tool_call(self):
        payloads = []
        def transport(payload, cancellation):
            payloads.append(json.loads(json.dumps(payload)))
            if len(payloads) == 1:
                yield {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "a", "function": {"name": "read", "arguments": "{\"path\": \"a.txt\"}"}}]},
                    "finish_reason": "tool_calls"}]}
            else:
                yield {"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}

        def read(arguments, context):
            arguments["path"] = "/etc/passwd"      # normalising in place, as handlers often do
            arguments.setdefault("encoding", "utf-8")
            return "ok"

        agent = Agent(model="m", provider=ChatCompletionsProvider("k", transport),
                      tools=[Tool("read", "read", {"type": "object"}, read)])
        session = Session(agent)
        session.run("go")
        sent = next(m for m in payloads[1]["messages"] if m.get("tool_calls"))
        self.assertEqual(json.loads(sent["tool_calls"][0]["function"]["arguments"]), {"path": "a.txt"})


if __name__ == "__main__":
    unittest.main()
