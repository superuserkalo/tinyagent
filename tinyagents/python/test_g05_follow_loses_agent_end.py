"""F23: a follower must receive the run's agent_end. When the (non-detached)
prompt client vanishes and a write to it fails before the 50 ms disconnect
watcher notices, the emit callback raises, the loop is closed without ever
producing agent_end, and followers are closed with nothing terminal."""
import http.client, json, os, socket, struct, sys, threading, time, unittest
from tinyagent import Agent, AgentServer, MemoryStorage, ProviderEvent, ProviderTurn


class Chatty:
    """Streams small deltas quickly until cancelled."""
    def stream(self, model, instructions, history, tools, cancellation, extra=None):
        for _ in range(20000):
            cancellation.raise_if_cancelled()
            time.sleep(0.001)
            yield ProviderEvent("text_delta", "x" * 64)
        return ProviderTurn("x", {"role": "assistant", "content": [{"type": "text", "text": "x"}]}, [], [])


class FollowTerminal(unittest.TestCase):
    def _attempt(self, port):
        def call(method, path, body=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request(method, path, body=None if body is None else json.dumps(body),
                               headers={"Content-Type": "application/json"})
            data = connection.getresponse().read().decode()
            connection.close()
            return data
        session_id = json.loads(call("POST", "/sessions"))["sessionId"]
        body = json.dumps({"message": "go"}).encode()
        prompt = socket.create_connection(("127.0.0.1", port))
        prompt.sendall(f"POST /sessions/{session_id}/prompts HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                       f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        prompt.recv(4096)  # the run is streaming
        followed = {}

        def follow():
            followed["lines"] = [json.loads(line) for line in
                                 call("GET", f"/sessions/{session_id}/events?after=0&follow=1").splitlines()
                                 if line.startswith("{")]

        follower = threading.Thread(target=follow, daemon=True)
        follower.start()
        time.sleep(0.3)
        # Abrupt disconnect (RST), as when a client process is killed.
        prompt.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        prompt.close()
        follower.join(10)
        self.assertFalse(follower.is_alive(), "follow stream never ended")
        return followed["lines"]

    def test_g05_follower_always_gets_agent_end(self):
        with AgentServer(Agent(model="m", provider=Chatty()), port=0, storage=MemoryStorage()) as server:
            port = int(server.url.rsplit(":", 1)[1])
            for attempt in range(8):
                lines = self._attempt(port)
                self.assertTrue(lines and lines[-1]["type"] == "agent_end",
                                f"attempt {attempt}: follow stream ended on {lines[-1]['type'] if lines else None}")


if __name__ == "__main__":
    unittest.main()
