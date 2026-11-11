"""resume of a paused run with a non-string "error" (RPC resume_run / HTTP
/resume pass the client's JSON value through unchecked). The loop crashes on
output.encode after emitting tool_execution_start: no tool_execution_end, the
run fails as provider_error and the paused run is destroyed by a bad request."""
import os, sys, threading, unittest
from tinyagent import Agent, Tool, ScriptedProvider, RpcSession, MemoryStorage, pause


class ResumeErrorType(unittest.TestCase):
    def test_g04_bad_error_value_is_refused_and_pause_survives(self):
        provider = ScriptedProvider([[{"name": "ask", "arguments": {}}], "done"])
        agent = Agent(model="m", provider=provider,
                      tools=[Tool("ask", "ask", {"type": "object"}, lambda a, c: pause({"q": "ok?"}))])
        out = []
        rpc = RpcSession(agent, lambda value: out.append(value), lambda message: None, storage=MemoryStorage())
        rpc.handle({"id": "1", "type": "prompt", "message": "go"})
        self.assertTrue(rpc.wait_idle(5))
        self.assertIsNotNone(rpc.session.paused)
        del out[:]
        rpc.handle({"id": "2", "type": "resume_run", "error": 5})
        self.assertTrue(rpc.wait_idle(5))
        starts = [e for e in out if e.get("type") == "tool_execution_start"]
        ends = [e for e in out if e.get("type") == "tool_execution_end"]
        self.assertEqual(len(starts), len(ends), "tool_execution_start without tool_execution_end")
        self.assertIsNotNone(rpc.session.paused, "a malformed resume request destroyed the paused run")


if __name__ == "__main__":
    unittest.main()
