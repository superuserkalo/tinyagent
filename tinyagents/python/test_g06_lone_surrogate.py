"""A lone UTF-16 surrogate in model text (a JSON "\\ud83d" escape whose pair
arrives in another delta, or plain model garbage) cannot be encoded as UTF-8.
With FileStorage the failed append marks the whole Session failed; in RPC mode
the event write fails the connection and the server exits."""
import os, sys, tempfile, unittest
from rpc_pipe_fixture import Rpc
from tinyagent import Agent, Session, ScriptedProvider, MemoryStorage, AnthropicProvider


def transport_with_surrogate():
    calls = []
    def transport(payload, cancellation):
        calls.append(payload)
        text = "\ud83d" if len(calls) == 1 else "fine"
        yield {"type": "message_start", "message": {"id": "m", "role": "assistant", "content": []}}
        yield {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
        yield {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}
        yield {"type": "content_block_stop", "index": 0}
        yield {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
        yield {"type": "message_stop"}
    return transport


class LoneSurrogate(unittest.TestCase):
    def test_g06_session_stays_usable(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session(Agent(model="m", provider=AnthropicProvider("k", transport_with_surrogate())),
                              directory=directory)
            try:
                session.run("one")
            except Exception:
                pass
            if session.resumable:
                session.abandon()
            try:
                self.assertEqual(session.run("two").text, "fine")
            finally:
                session.close()

    def test_g06_rpc_server_survives(self):
        rpc = Rpc(Agent(model="m", provider=AnthropicProvider("k", transport_with_surrogate())),
                  storage=MemoryStorage())
        try:
            rpc.send({"id": "1", "type": "prompt", "message": "one"})
            rpc.wait_for(lambda line: line.get("type") == "agent_end", 3)
            rpc.send({"id": "2", "type": "get_state"})
            answer = rpc.wait_for(lambda line: line.get("id") == "2", 3)
            self.assertIsNotNone(answer, f"RPC server died: {rpc.failure!r}")
        finally:
            rpc.close()


if __name__ == "__main__":
    unittest.main()
