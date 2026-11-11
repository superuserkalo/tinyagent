"""An API key with a trailing newline (typical of a key read from a file or a
mounted secret) is echoed by http.client's header validation error, and that
text becomes agent_end.error.message and the run_failed journal record."""
import json, os, socket, sys, tempfile, threading, unittest
from tinyagent import Agent, Session, OpenAIProvider, AnthropicProvider

SECRET = "sk-SUPERSECRET-0123456789"


class KeyLeak(unittest.TestCase):
    def _leaks(self, make):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        provider = make(f"http://127.0.0.1:{port}/v1")
        with tempfile.TemporaryDirectory() as directory:
            agent = Agent(model="m", provider=provider, sleep=lambda s, c: None)
            session = Session(agent, directory=directory)
            events = []
            try:
                session.run("hi", on_event=lambda e: events.append(e.as_dict()))
            except Exception as error:
                events.append({"raised": str(error)})
            journal = open(os.path.join(directory, session.id + ".jsonl")).read()
            session.close()
        return json.dumps(events) + journal

    def test_g14_openai_key_not_in_events_or_journal(self):
        seen = self._leaks(lambda base: OpenAIProvider(SECRET + "\n", base_url=base, timeout=5))
        self.assertNotIn(SECRET, seen)

    def test_g14_anthropic_key_not_in_events_or_journal(self):
        seen = self._leaks(lambda base: AnthropicProvider(SECRET + "\n", base_url=base, timeout=5))
        self.assertNotIn(SECRET, seen)


if __name__ == "__main__":
    unittest.main()
