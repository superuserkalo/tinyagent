"""The interactive approval prompt prints the model's command verbatim. A
command carrying a carriage return and an erase-line escape hides its real
text, so the person approves something other than what runs."""
import builtins, contextlib, io, os, sys, tempfile, unittest
from unittest import mock
import tiny_agent
from tinyagent import ScriptedProvider

COMMAND = "echo pwned > /dev/null #\x1b[2K\rls"


class ApprovalPrompt(unittest.TestCase):
    def test_g13_prompt_shows_no_raw_control_characters(self):
        provider = ScriptedProvider([[{"name": "bash", "arguments": {"command": COMMAND}}], "done"])
        asked = []
        answers = iter(["do it"])

        def fake_input(prompt=""):
            if "Run it?" in prompt:
                asked.append(prompt)
                return "n"
            try:
                return next(answers)
            except StopIteration:
                raise EOFError

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(builtins, "input", fake_input), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            tiny_agent.main([], environ={"TINYAGENT_SESSION_DIR": directory}, provider=provider)
        self.assertEqual(len(asked), 1)
        self.assertNotIn("\x1b", asked[0])
        self.assertNotIn("\r", asked[0])


if __name__ == "__main__":
    unittest.main()
