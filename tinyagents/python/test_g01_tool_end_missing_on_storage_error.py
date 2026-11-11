"""A tool has started (tool_execution_start emitted) and finished; the append
of its tool_result fails (fenced by a second writer, or a storage error). The
stream then ends with agent_end and no tool_execution_end for the call."""
import os, sys, unittest
from tinyagent import Agent, Session, Tool, ScriptedProvider, MemoryStorage, SessionFencedError


class FailingStorage(MemoryStorage):
    def __init__(self, kind, error):
        super().__init__()
        self.kind, self.error = kind, error

    def append(self, session_id, expected, record):
        if record.get("type") == self.kind:
            raise self.error
        return super().append(session_id, expected, record)


class ToolEnd(unittest.TestCase):
    def _events(self, error):
        provider = ScriptedProvider([[{"name": "act", "arguments": {}}], "done"])
        agent = Agent(model="m", provider=provider, tools=[Tool("act", "act", {"type": "object"}, lambda a: "ok")])
        session = Session(agent, storage=FailingStorage("tool_result", error))
        events = []
        try:
            session.run("go", on_event=lambda e: events.append(e.as_dict()))
        except Exception:
            pass
        return events

    def _check(self, events):
        starts = [e["toolCallId"] for e in events if e["type"] == "tool_execution_start"]
        ends = [e["toolCallId"] for e in events if e["type"] == "tool_execution_end"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts, ends, "a started tool call got no tool_execution_end")

    def test_g01_fenced_result_write(self):
        self._check(self._events(SessionFencedError("another writer appended")))

    def test_g01_failed_result_write(self):
        self._check(self._events(OSError("disk full")))


if __name__ == "__main__":
    unittest.main()
