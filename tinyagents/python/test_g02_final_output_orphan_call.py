"""final_output called together with another tool call: the run completes at
final_output and commits an assistant turn whose second call has no result.
Every later prompt then sends an invalid history (call without result)."""
import os, sys, unittest
from tinyagent import Agent, Session, Tool, ScriptedProvider

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


class FinalOutputOrphan(unittest.TestCase):
    def test_g02_committed_history_has_no_call_without_result(self):
        ran = []
        provider = ScriptedProvider([
            [{"id": "c_final", "name": "final_output", "arguments": {"answer": "done"}},
             {"id": "c_note", "name": "note", "arguments": {}}],
            "second answer",
        ])
        agent = Agent(model="m", provider=provider,
                      tools=[Tool("note", "note", {"type": "object"}, lambda a: ran.append(1) or "ok")])
        session = Session(agent)
        result = session.run("go", output_schema=SCHEMA)
        self.assertEqual(result.value, {"answer": "done"})
        session.run("next")
        sent = provider.requests[-1]["history"]
        calls = [b["callId"] for m in sent if m.get("role") == "assistant"
                 for b in m["content"] if b.get("type") == "tool_call"]
        results = [m["callId"] for m in sent if m.get("role") == "tool_result"]
        self.assertEqual(sorted(calls), sorted(results),
                         "history sent to the provider has a tool call without a result")


if __name__ == "__main__":
    unittest.main()
