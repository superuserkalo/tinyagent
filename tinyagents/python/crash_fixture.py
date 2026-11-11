"""A process that dies at a named point of a run, for the resume tests.

    TINYAGENT_TEST_CRASH_AT=before_intent|after_intent|after_result|after_reply
    python crash_fixture.py <directory> <session-id> [idempotent]

Only this fixture reads that variable; the shipped CLI does not.
"""

import os
import sys

from tinyagent import Agent, FileStorage, Session, Tool
from tinyagent_testing import ScriptedProvider

EXIT_CODE = 41


class CrashingStorage(FileStorage):
    """FileStorage that kills the process around the record that names the crash point."""

    crash_at = os.environ.get("TINYAGENT_TEST_CRASH_AT", "")

    def append(self, session_id, expected, record):
        kind = record["type"]
        item = record.get("item", {})
        if self.crash_at == "before_intent" and kind == "tool_intent":
            os._exit(EXIT_CODE)
        sequence = super().append(session_id, expected, record)
        final_reply = (kind == "item" and item.get("role") == "assistant"
                       and not any(block["type"] == "tool_call" for block in item["content"]))
        if ((self.crash_at == "after_intent" and kind == "tool_intent")
                or (self.crash_at == "after_result" and kind == "tool_result")
                or (self.crash_at == "after_reply" and final_reply)):
            os._exit(EXIT_CODE)
        return sequence


def build_agent(directory, idempotent, provider):
    """The agent both the crashing process and the resuming test use."""

    def effect(arguments, context):
        with open(os.path.join(directory, "effects.log"), "a") as log:
            log.write(context.execution_id + "\n")
        return "effect done"

    return Agent(model="offline", provider=provider,
                 tools=[Tool("effect", "Do something once.", {"type": "object"}, effect, idempotent=idempotent)])


SCRIPT = [[{"name": "effect", "id": "call_1"}], "all done"]

if __name__ == "__main__":
    directory, session_id = sys.argv[1], sys.argv[2]
    agent = build_agent(directory, "idempotent" in sys.argv[3:], ScriptedProvider(SCRIPT))
    session = Session(agent, storage=CrashingStorage(directory), session_id=session_id)
    session.run("do it", message_id="m1")
