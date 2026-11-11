"""The consumer closes the stream while handling a failed compaction_end.
Session._prepare yields again inside its finally, so close() raises
"generator ignored GeneratorExit", the cleanup that writes run_failed is
skipped, and the run stays open in the journal with no terminal state."""
import os, sys, unittest
from tinyagent import Agent, Session, ScriptedProvider, ContextBudget


class CompactionClose(unittest.TestCase):
    def test_g08_closing_at_failed_compaction_end_settles_the_run(self):
        budget = ContextBudget(50, 1, lambda request: 100 if len(request["history"]) >= 5 else 1, keep_turns=1)
        # Third prompt needs compaction; the summary comes back empty, so compaction fails.
        provider = ScriptedProvider(["a", "b", ""])
        session = Session(Agent(model="m", provider=provider), budget=budget)
        session.run("one")
        session.run("two")
        events = session.stream("three")
        seen = []
        raised = None
        for event in events:
            seen.append(event.type)
            if event.type == "compaction_end":
                try:
                    events.close()
                except BaseException as error:
                    raised = error
                break
        self.assertIn("compaction_end", seen)
        self.assertIsNone(raised, f"closing the stream raised {raised!r}")
        kinds = [record["type"] for record in session.records]
        self.assertEqual(kinds[-1], "run_failed", f"run left open in the journal: {kinds}")
        self.assertFalse(session.resumable)


if __name__ == "__main__":
    unittest.main()
