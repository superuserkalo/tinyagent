"""One session from start to finish.

It registers a tool, streams a run, cancels a run, restarts, resumes the
session from disk, compacts it, and continues.

    export OPENAI_API_KEY=...
    python examples/quickstart.py [session-directory]

The script runs as two processes. The first creates the session and exits.
The second reopens it, as your application would after a restart.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from contextlib import closing

from tinyagent import (
    Agent,
    AgentEvent,
    CancellationToken,
    CancelledError,
    ContextBudget,
    OpenAIProvider,
    Session,
    Tool,
)

NOTES = {
    "release": (
        "Release 0.0.1 checklist. Done: the test suite passes on every supported "
        "Python version, the wheel builds, and the getting-started guide is written. "
        "Still open: write the changelog entry, and upload the wheel. The changelog "
        "must be reviewed by a second person before the upload."
    ),
}


def read_notes(arguments: dict) -> str:
    # Arguments were validated against the schema below before this call.
    return NOTES[arguments["topic"]]


def build_agent() -> Agent:
    # A saved session only reopens with the same model, instructions, and
    # tools, so both processes build the agent the same way.
    notes = Tool(
        name="read_notes",
        description="Read the team's notes on a topic.",
        parameters={
            "type": "object",
            "properties": {"topic": {"type": "string", "enum": sorted(NOTES)}},
            "required": ["topic"],
            "additionalProperties": False,
        },
        handler=read_notes,
    )
    # QUICKSTART_ENDPOINT points the example at another Responses-compatible
    # server. The offline test uses it to supply a local fake.
    endpoint = os.getenv("QUICKSTART_ENDPOINT")
    return Agent(
        model=os.getenv("TINYAGENT_MODEL", "gpt-5.6"),
        instructions="You help a small team ship a release. Use read_notes for facts.",
        tools=[notes],
        provider=OpenAIProvider(endpoint=endpoint) if endpoint else OpenAIProvider(),
    )


# TinyAgent has no tokenizer. count_tokens receives the complete request
# (model, instructions, history, tools); four bytes per token is a rough guess.
BUDGET = ContextBudget(
    max_tokens=50_000,
    response_reserve=4_000,
    count_tokens=lambda request: len(json.dumps(request)) // 4,
    keep_turns=1,
)


def show(event: AgentEvent) -> None:
    if event.type == "text_delta":
        print(event.data["delta"], end="", flush=True)
    elif event.type == "tool_execution_start":
        print(f"[tool {event.data['toolName']} {json.dumps(event.data['arguments'])}]")
    elif event.type == "agent_end":
        print(f"\n[run {event.data['status']}]")
    elif event.type == "compaction_end":
        print(f"[compaction {event.data['status']}]")


def start(directory: str) -> str:
    with Session(build_agent(), directory=directory, budget=BUDGET) as session:
        # Stream a run. closing() releases the session if the loop stops early.
        with closing(session.stream("Read the release notes. What is left to do?")) as events:
            for event in events:
                show(event)

        # Cancel a run from its own event handler, as soon as it starts to answer.
        token = CancellationToken()

        def cancel_on_first_text(event: AgentEvent) -> None:
            show(event)
            if event.type == "text_delta":
                token.cancel()

        try:
            session.run(
                "Check the notes again, then write a long essay about release management.",
                cancellation=token,
                on_event=cancel_on_first_text,
            )
        except CancelledError:
            # The journal records the cancelled run. History does not include it.
            print(f"cancelled; history still has {len(session.history)} messages")
        return session.id


def resume(directory: str, session_id: str) -> None:
    with Session.open(build_agent(), directory, session_id, budget=BUDGET) as session:
        print(f"resumed {session.id} with {len(session.history)} messages")

        if session.resumable:
            # The first process died in the middle of a run. Pick it up where
            # the journal stops instead of starting over.
            session.resume(on_event=show)

        session.run("Which open item comes first?", on_event=show)

        # Summarize everything except the latest turn. The journal keeps the
        # full history; only the context sent to the model gets shorter.
        session.compact(on_event=show)

        result = session.run("Give me the plan in one sentence.", on_event=show)
        print(f"final answer: {result.text}")
        print(f"{len(session.history)} messages in history, {len(session.records)} records on disk")


def main() -> None:
    if sys.argv[1:2] == ["resume"]:
        resume(sys.argv[2], sys.argv[3])
        return
    directory = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="tinyagent-quickstart-")
    session_id = start(directory)
    print(f"session {session_id} saved in {directory}; restarting", flush=True)
    subprocess.run([sys.executable, __file__, "resume", directory, session_id], check=True)


if __name__ == "__main__":
    main()
