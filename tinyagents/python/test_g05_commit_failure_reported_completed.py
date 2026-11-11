"""The run_commit append is fenced (a second writer got there first). RPC holds
the terminal event until after the commit attempt, yet still delivers
agent_end status "completed". The journal has no commit for that run."""
import os, sys, unittest
from tinyagent import Agent, ScriptedProvider, RpcSession, MemoryStorage, SessionFencedError, AgentServer
import http.client, json


class FencedCommit(MemoryStorage):
    def append(self, session_id, expected, record):
        if record.get("type") == "run_commit":
            raise SessionFencedError("another writer appended to this session")
        return super().append(session_id, expected, record)


class CommitFailure(unittest.TestCase):
    def test_g05_rpc_does_not_report_completed(self):
        storage = FencedCommit()
        agent = Agent(model="m", provider=ScriptedProvider(["hello"]))
        out = []
        rpc = RpcSession(agent, lambda value: out.append(value), lambda message: None, storage=storage)
        rpc.handle({"id": "1", "type": "prompt", "message": "go"})
        self.assertTrue(rpc.wait_idle(5))
        end = [e for e in out if e.get("type") == "agent_end"][-1]
        committed = any(r["type"] == "run_commit" for r in storage.load(rpc.session.id))
        self.assertFalse(committed)
        self.assertNotEqual(end["status"], "completed",
                            "client was told the run completed although its commit was fenced")

    def test_g05_http_does_not_report_completed(self):
        storage = FencedCommit()
        agent = Agent(model="m", provider=ScriptedProvider(["hello"]))
        with AgentServer(agent, port=0, storage=storage) as server:
            port = int(server.url.rsplit(":", 1)[1])
            def call(method, path, body=None):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                connection.request(method, path, body=None if body is None else json.dumps(body),
                                   headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                data = response.read().decode()
                connection.close()
                return data
            session_id = json.loads(call("POST", "/sessions"))["sessionId"]
            lines = [json.loads(line) for line in
                     call("POST", f"/sessions/{session_id}/prompts", {"message": "go"}).splitlines() if line]
        end = [e for e in lines if e.get("type") == "agent_end"][-1]
        self.assertNotEqual(end["status"], "completed",
                            "client was told the run completed although its commit was fenced")


if __name__ == "__main__":
    unittest.main()
