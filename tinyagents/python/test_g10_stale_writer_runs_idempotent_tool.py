"""B1: "a fenced writer never executes the tool". On resume, a pending
idempotent call is rerun without any write first, so a writer whose view is
stale (another writer already resumed and committed the run) executes the tool
and only learns it is fenced when it appends the result."""
import os, sys, unittest
from tinyagent import Agent, Session, Tool, ScriptedProvider, MemoryStorage


class CrashAtResult(MemoryStorage):
    crash = True
    def append(self, session_id, expected, record):
        if self.crash and record.get("type") == "tool_result":
            raise OSError("process died here")
        return super().append(session_id, expected, record)


class StaleWriter(unittest.TestCase):
    def test_g10_fenced_writer_does_not_execute(self):
        runs = []
        tool = Tool("put", "put", {"type": "object"}, lambda a, c: runs.append(c.execution_id) or "ok",
                    idempotent=True)
        storage = CrashAtResult()
        first = Session(Agent(model="m", provider=ScriptedProvider([[{"name": "put", "arguments": {}}]]),
                              tools=[tool]), storage=storage)
        try:
            first.run("go")
        except Exception:
            pass
        storage.crash = False
        session_id = first.id
        del runs[:]
        # Two writers load the same open run.
        a = Session.open(Agent(model="m", provider=ScriptedProvider(["done"]), tools=[tool]), storage, session_id)
        b = Session.open(Agent(model="m", provider=ScriptedProvider(["done"]), tools=[tool]), storage, session_id)
        a.resume()
        self.assertEqual(len(runs), 1)
        try:
            b.resume()
        except Exception:
            pass
        self.assertEqual(len(runs), 1, "the fenced (stale) writer executed the tool before being fenced")


if __name__ == "__main__":
    unittest.main()
