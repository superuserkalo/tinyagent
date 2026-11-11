"""The 131,072-byte tool output limit does not apply to the result a client
supplies to a paused call, so an oversized observation enters events and
committed history (PROTOCOL: "its full content is never placed in history or
an event")."""
import json, os, sys, unittest
from tinyagent import Agent, Session, Tool, ScriptedProvider, pause, MAX_TOOL_OUTPUT_BYTES


class ResumeOversize(unittest.TestCase):
    def test_g04_resume_result_respects_tool_output_limit(self):
        provider = ScriptedProvider([[{"name": "ask", "arguments": {}}], "done"])
        agent = Agent(model="m", provider=provider,
                      tools=[Tool("ask", "ask", {"type": "object"}, lambda a, c: pause())])
        session = Session(agent)
        self.assertEqual(session.run("go").status, "paused")
        events = []
        # Ruling H6: an oversized result is refused before anything starts, and the pause is untouched.
        with self.assertRaisesRegex(ValueError, "exceeds"):
            session.resume(result="x" * (MAX_TOOL_OUTPUT_BYTES * 4), on_event=lambda e: events.append(e.as_dict()))
        self.assertEqual((events, session.paused is not None), ([], True))
        session.resume(result="small", on_event=lambda e: events.append(e.as_dict()))
        biggest_event = max(len(json.dumps(e)) for e in events)
        biggest_item = max(len(json.dumps(item)) for item in session.history)
        self.assertLessEqual(biggest_event, MAX_TOOL_OUTPUT_BYTES + 4096, "oversized observation in an event")
        self.assertLessEqual(biggest_item, MAX_TOOL_OUTPUT_BYTES + 4096, "oversized observation in history")


if __name__ == "__main__":
    unittest.main()
