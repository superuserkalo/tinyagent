"""A command that leaves a child in its own session holding the output pipes
(a daemonised server, `setsid x &`) blocks run_bash in reader.join() after the
shell has exited. killpg cannot reach the child, so cancellation never wins and
a run without a tool timeout hangs until the child exits by itself."""
import os, sys, threading, time, unittest
from tinyagent import Agent, Session, ScriptedProvider, CancellationToken, CancelledError, bash_tool

HOLD = 12
COMMAND = (f"{sys.executable} -c 'import os,time; os.setsid(); time.sleep({HOLD})' & echo started")


@unittest.skipUnless(os.name == "posix", "posix only")
class DetachedChild(unittest.TestCase):
    def test_g12_cancellation_wins_within_bounded_time(self):
        provider = ScriptedProvider([[{"name": "bash", "arguments": {"command": COMMAND}}], "done"])
        session = Session(Agent(model="m", provider=provider, tools=[bash_tool()]))
        cancellation = CancellationToken()
        finished = threading.Event()

        def work():
            try:
                session.run("go", cancellation=cancellation)
            except CancelledError:
                pass
            finally:
                finished.set()

        began = time.monotonic()
        threading.Thread(target=work, daemon=True).start()
        time.sleep(1.0)
        cancellation.cancel()
        self.assertTrue(finished.wait(5), "run did not stop within 5 s of cancellation")
        self.assertLess(time.monotonic() - began, 7)

    def test_g12_tool_returns_when_the_shell_has_exited(self):
        # No cancellation at all: the shell exits at once, yet the tool call does not return.
        provider = ScriptedProvider([[{"name": "bash", "arguments": {"command": COMMAND}}], "done"])
        session = Session(Agent(model="m", provider=provider, tools=[bash_tool()]))
        finished = threading.Event()
        threading.Thread(target=lambda: (session.run("go"), finished.set()), daemon=True).start()
        self.assertTrue(finished.wait(5), "bash tool still blocked 5 s after its shell exited")


if __name__ == "__main__":
    unittest.main()
