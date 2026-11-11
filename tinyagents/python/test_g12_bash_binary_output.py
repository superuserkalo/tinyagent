"""Bash output that is not UTF-8 is decoded with U+FFFD, three bytes per bad
byte, after truncation. 30,000 kept bytes per stream become up to 90,000, so
stdout plus stderr exceed the generic 131,072 limit and the whole result is
replaced by ToolOutputLimitError, although A6 says large output is not an error."""
import os, sys, unittest
from tinyagent import Agent, Session, ScriptedProvider, bash_tool

COMMAND = (f"{sys.executable} -c \"import sys; b=bytes([255])*40000; "
           "sys.stdout.buffer.write(b); sys.stderr.buffer.write(b)\"")


class BinaryOutput(unittest.TestCase):
    def test_g12_binary_output_is_truncated_not_an_error(self):
        provider = ScriptedProvider([[{"name": "bash", "arguments": {"command": COMMAND}}], "done"])
        events = []
        Session(Agent(model="m", provider=provider, tools=[bash_tool()])).run(
            "go", on_event=lambda e: events.append(e.as_dict()))
        end = next(e for e in events if e["type"] == "tool_execution_end")
        self.assertFalse(end["isError"], end["output"][:200])
        self.assertTrue(end["output"].startswith("exit_code: 0"))


if __name__ == "__main__":
    unittest.main()
