# TinyAgent for Python

A small agent SDK. It calls OpenAI Responses or Anthropic Messages directly,
runs your tools, streams events, and keeps a session on disk so work can
continue after a restart. There is one agent loop and one session
implementation, and both are short enough to read.

You work with three things:

- **Tool**: a function the model may call, with a JSON Schema for its arguments.
- **Agent**: a model, instructions, a provider, and tools. It holds no conversation.
- **Session**: one conversation. It owns history, saves it, resumes it, and compacts it.

## Install

Python 3.10 or newer. The package is not on PyPI yet, so install it from a clone:

```sh
python -m pip install ./tinyagents/python
```

It depends on `jsonschema` and `referencing`, which validate tool arguments.
Importing `tinyagent` does not read the environment, print, or start anything.

## A tool and an agent

```python
from tinyagent import Agent, Tool

def read_notes(arguments):
    return "Still open: write the changelog entry, and upload the wheel."

notes = Tool(
    name="read_notes",
    description="Read the team's notes on a topic.",
    parameters={
        "type": "object",
        "properties": {"topic": {"type": "string"}},
        "required": ["topic"],
        "additionalProperties": False,
    },
    handler=read_notes,
)

agent = Agent(
    model="gpt-5.6",
    instructions="You help a small team ship a release.",
    tools=[notes],
)
```

The schema is JSON Schema Draft 2020-12. Arguments are validated before the
handler runs. Invalid arguments, and exceptions raised by a handler, go back to
the model as a tool error so it can try again. A handler may take a second
argument, a `CancellationToken`. A handler returns a string or a value that
`json.dumps` accepts. Tools run one at a time.

`Agent` uses `OpenAIProvider` and reads `OPENAI_API_KEY` when a request starts.
For Anthropic, pass a provider; it reads `ANTHROPIC_API_KEY`:

```python
from tinyagent import Agent, AnthropicProvider

agent = Agent(model="claude-sonnet-4-6", provider=AnthropicProvider(), tools=[notes])
```

## A session

```python
from tinyagent import Session

with Session(agent, directory="sessions") as session:
    result = session.run("What is left to do?")
    print(result.text)
    print(session.id)  # keep this to resume later
```

With `directory`, every step is written to `sessions/<id>.jsonl` before the
next one happens. Without it, the session lives in memory only.

`agent.run(prompt, history)` also exists. It is the loop without a session:
you pass prior history in and get the new history back.

## Streaming

`session.run` takes an `on_event` callback. `session.stream` gives you the same
events as an iterator.

```python
from contextlib import closing

with closing(session.stream("What is left to do?")) as events:
    for event in events:
        if event.type == "text_delta":
            print(event.data["delta"], end="", flush=True)
```

Use `closing` because Python does not close an iterator when a `for` loop
exits early, and the session stays busy until its stream is closed.

Every event has `type`, `run_id`, `sequence` (from 1, in order), and `data`:

| Event | `data` |
| --- | --- |
| `agent_start` | |
| `text_delta` | `delta` |
| `tool_execution_start` | `toolCallId`, `toolName`, `arguments` |
| `tool_execution_end` | `toolCallId`, `toolName`, `output`, `isError` |
| `compaction_start` | |
| `compaction_end` | `status` (`completed`, `failed`, `aborted`), then `changed` or `error` |
| `agent_end` | `status` (`completed`, `failed`, `aborted`), then `text` or `error` |

If your callback raises, the run stops and you get your own exception back.

## Cancelling

```python
from tinyagent import CancellationToken, CancelledError

token = CancellationToken()
try:
    session.run("Write a long essay.", cancellation=token)
except CancelledError:
    pass
```

Call `token.cancel()` from an event callback or another thread. It closes the
provider request and is passed to the running tool. A cancelled or failed run
is recorded in the journal but is not added to `session.history`.

## Restart and resume

```python
session = Session.open(agent, "sessions", session_id)
```

`Session.list("sessions")` returns the saved IDs. Build the agent the same way
before you reopen: a different model, instructions, or tool list is refused
with `ValueError`.

A session has one writer. Opening it a second time, in this process or another,
raises `SessionBusyError`. The lock is released when the session is closed or
its process exits. Closing never deletes anything.

`session.history` is the committed conversation. `session.records` is
everything in the journal, including runs that did not finish.
`session.inspect(offset=0, limit=20, view="history")` reads either in pages.

### After an interrupted tool

Before a tool runs, the session records the intent. If the run then stops for
any reason before it completes (a crash, a cancellation, an error), TinyAgent
cannot know whether the tool's effect happened, and it will not run the tool
again on its own. `session.recovery_required` is `True`, and `run` and
`compact` raise `RecoveryRequiredError` until you look and say what you found:

```python
if session.recovery_required:
    print(session.records[-3:])
    session.reconcile("The email was sent. Do not send it again.")
```

The note is added to history as a user message, so the model sees it.

### What can go wrong with the file

- A final line cut off mid-write is dropped when the session is opened.
- A complete line that is invalid raises `SessionCorruptionError`. The file is
  left as it is for you to inspect.
- If a write fails, the open session refuses further work. Close it and reopen
  it. A record whose write was reported as failed may still be on disk, and is
  then read back on reopen.

## Compaction

Compaction asks the model to summarize older turns, and sends the summary in
their place. The journal and `session.history` keep the full conversation.

```python
import json
from tinyagent import ContextBudget, Session

budget = ContextBudget(
    max_tokens=50_000,
    response_reserve=4_000,
    count_tokens=lambda request: len(json.dumps(request)) // 4,
    keep_turns=2,
)
session = Session(agent, directory="sessions", budget=budget)
session.compact()  # True if a new summary was installed
```

TinyAgent has no tokenizer. `count_tokens` receives the whole request (`model`,
`instructions`, `history`, `tools`) and returns your estimate. The last
`keep_turns` turns are never summarized.

With a budget, a session also compacts by itself before any model request that
would exceed `max_tokens` less `response_reserve`. A summary is saved before it
is used. If the summary request fails, is cancelled, is empty, or does not save
room, nothing changes and the error reaches you. If the request still does not
fit, the run fails with a message saying so; it does not drop history or retry.

## The complete example

[`examples/quickstart.py`](examples/quickstart.py) does all of the above in one
script: it registers a tool, streams a run, cancels a run, exits, resumes the
session in a second process, reconciles, compacts, and continues.

```sh
export OPENAI_API_KEY=...
python examples/quickstart.py
```

`test_example.py` runs the same file against a local fake provider, so it is
checked on every test run without a key or network access.

## Command line, RPC, and HTTP

These are optional and use the same `Agent` and `Session`.

```sh
tinyagent                      # interactive coding agent with a Bash tool
tinyagent --list-sessions
tinyagent --resume s_...
tinyagent --mode rpc           # JSONL commands on stdin, events on stdout
tinyagent serve                # HTTP on 127.0.0.1:8080
```

From a clone, `python tiny_agent.py` does the same. The CLI reads
`OPENAI_API_KEY`, or `ANTHROPIC_API_KEY` with `TINYAGENT_PROVIDER=anthropic`.
`TINYAGENT_MODEL` picks the model. Sessions go in `.tinyagent/sessions` under
the current directory, or `TINYAGENT_SESSION_DIR`. `TINYAGENT_CONTEXT_TOKENS`
turns on compaction with a rough byte-count estimate, and
`TINYAGENT_RESPONSE_RESERVE` sets the reserve (4096 by default). In the
interactive prompt, `/history`, `/compact`, and `/reconcile <note>` call the
session methods of the same name.

The CLI's Bash tool runs commands the model writes. Its list of denied commands
catches obvious mistakes. It is not a sandbox.

To serve your own agent and tools:

```python
from tinyagent import serve

with serve(agent, port=8080, directory="sessions", budget=budget) as server:
    print(server.url)
    ...  # keep the process alive
```

`serve` binds to loopback. Any other host needs `allow_remote=True` and a
`token`. `run_rpc(agent, directory="sessions")` does the same for JSONL over
stdin and stdout. [PROTOCOL.md](../../PROTOCOL.md) describes both wire formats.

Session commands over RPC are `compact`, `list_sessions`, `resume_session`,
`get_history`, and `reconcile`, next to `prompt`, `abort`, `new_session`, and
`get_state`. Over HTTP they are `GET /sessions`, `POST /sessions` with an
optional `{"sessionId": ...}` to reopen a saved session, `GET /sessions/:id`
to page through it, and `POST /sessions/:id/compact` and `/reconcile`.
`DELETE /sessions/:id` closes a session and keeps its file.

## Things to know

- Providers and tools are synchronous. Use a thread per session if you need
  several conversations at once; one session runs one thing at a time and
  raises `SessionBusyError` otherwise.
- Tool output and tool errors are limited to 131,072 UTF-8 bytes. Larger output
  becomes a tool error.
- CI runs the tests on Linux and macOS. Windows is not tested.

## Tests

From this directory, with the package installed:

```sh
python -m unittest discover -p 'test_*.py'
python tiny_agent.py --self-test
```

The tests need no API key and no network beyond loopback. They read the shared
fixtures in `../../conformance`, and write only to the system temp directory.
