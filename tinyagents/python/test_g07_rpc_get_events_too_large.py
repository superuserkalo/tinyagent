"""get_events builds an unbounded page (get_history bounds its page to fit a
line). A session with a few large tool outputs makes the default get_events
exceed the 1 MiB line limit: the command gets an uncorrelated protocol_error
instead of a response with its id."""
import os, sys, unittest
from rpc_pipe_fixture import Rpc
from tinyagent import Agent, Tool, ScriptedProvider, MemoryStorage


class GetEvents(unittest.TestCase):
    def test_g07_default_get_events_is_answered(self):
        turns = [[{"name": "big", "arguments": {}}] for _ in range(10)] + ["done"]
        agent = Agent(model="m", provider=ScriptedProvider(turns),
                      tools=[Tool("big", "big", {"type": "object"}, lambda a: "x" * 120_000)])
        rpc = Rpc(agent, storage=MemoryStorage())
        try:
            rpc.send({"id": "1", "type": "prompt", "message": "go"})
            self.assertIsNotNone(rpc.wait_for(lambda line: line.get("type") == "agent_end", 10))
            rpc.send({"id": "2", "type": "get_events"})
            answer = rpc.wait_for(lambda line: line.get("id") == "2" or line.get("type") == "protocol_error", 5)
            self.assertIsNotNone(answer)
            self.assertEqual(answer.get("id"), "2", f"get_events was not answered: {answer}")
            self.assertTrue(answer.get("success"), answer)
        finally:
            rpc.close()


if __name__ == "__main__":
    unittest.main()
