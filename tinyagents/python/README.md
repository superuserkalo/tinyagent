# TinyAgent for Python

A small agent SDK. It calls OpenAI Responses or Anthropic Messages directly,
runs your tools, streams events, and keeps a session on disk so work can
continue after a restart. There is one agent loop and one session
implementation, and both are short enough to read.

You work with three things:

- **Tool**: a function the model may call, with a JSON Schema for its arguments.
- **Agent**: a model, instructions, a provider, and tools. It holds no conversation.
- **Session**: one conversation. It owns history, saves it, resumes it, and compacts it.

There are two extension points: **Storage**, where a session keeps its records,
and **Provider**, which talks to a model API. This package implements protocol
version 2 of [PROTOCOL.md](../../PROTOCOL.md).

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
handler runs. Invalid arguments, a call to a tool that does not exist
(`Unknown tool: <name>`), and exceptions raised by a handler go back to the
model as a tool error so it can try again. Raise `ToolError("text")` to choose
the exact error text. A handler returns a string, a value that `json.dumps`
accepts, or a list of content parts (see [Images](#images)). Tools run one at
a time.

### The handler's second argument

Write handlers as `handler(arguments, context)`. One-argument handlers keep
working. `context` is a `ToolContext`:

| Attribute | |
| --- | --- |
| `context.cancellation` | The call's `CancellationToken`. Check it in long work. |
| `context.execution_id` | Stable name for this call: `"<session id>:<sequence of its intent record>"`, or the provider's call ID outside a session. It is the same when the call is run again after a crash, so pass it to APIs that take an idempotency key. |
| `context.context` | The value you passed as `context=` to the run. |
| `context.timed_out` | `True` once the tool's `timeout_seconds` has passed. |

Handlers written for version 1 treated the second argument as the token.
They still work: the context also answers `cancelled`, `raise_if_cancelled()`
and `on_cancel()`.

`Tool(..., idempotent=True)` says the handler is safe to run twice with the same
`execution_id`; see [After a crash](#after-a-crash). `Tool(...,
timeout_seconds=30)` cancels `context.cancellation` after that long (at most
86,400). The handler then has one second to return (what it returns or raises
is used); after that the call becomes the tool error `timed out after 30 s`
and the run moves on.

Handlers, hooks and providers each run in a thread of their own, so that one
which never returns cannot hold a cancelled run. After a cancellation they get
one second to settle; then the run is recorded as aborted and the session is
released, and the thread is left behind. Honour `context.cancellation`.
A handler receives a copy of the arguments: changing them changes nothing else.
Only the run's own cancellation makes a run `aborted`. A handler, hook or
provider that raises `CancelledError` for reasons of its own is an ordinary
tool, hook or provider error.

### Providers

`Agent` uses `OpenAIProvider` (the Responses API) and reads `OPENAI_API_KEY`
when a request starts, never earlier. Pass another provider to change that:

```python
from tinyagent import Agent, AnthropicProvider, ChatCompletionsProvider

agent = Agent(model="claude-sonnet-4-6", provider=AnthropicProvider(), tools=[notes])
local = Agent(model="llama3", provider=ChatCompletionsProvider("unused", base_url="http://localhost:11434/v1"))
```

`AnthropicProvider` reads `ANTHROPIC_API_KEY`. `ChatCompletionsProvider` speaks
the OpenAI-compatible `/chat/completions` streaming API that most gateways and
self-hosted servers offer; it reads `OPENAI_API_KEY` unless given a key, and
tolerates servers that send no usage or send tool arguments in fragments.

All three take `base_url` (the API root, such as `https://api.openai.com/v1`),
`headers` (added to the request; a header of the same name replaces the
default, including the API key header), `extra` (a dict merged into every
request body after TinyAgent's own fields: temperature, reasoning effort,
maximum output tokens, cache controls), and `timeout`.

### Limits, retries and usage

```python
agent = Agent(model="gpt-5.6", tools=[notes], max_steps=20, max_seconds=300, max_tokens=200_000)
result = Session(agent).run("What is left to do?")
print(result.usage)  # {"inputTokens": 812, "outputTokens": 96, "requests": 2}
```

`max_steps` counts model requests in one run, `max_seconds` wall time, and
`max_tokens` input plus output tokens. They are checked before each model
request. A run that reaches one raises `LimitExceededError` (`.limit` is
`steps`, `seconds` or `tokens`) and ends with `agent_end` `status: "failed"`,
`error.code: "limit_exceeded"`. After three model turns in a row in which every
tool call failed, the run raises `RepeatedToolErrorsError`
(`repeated_tool_errors`). Both are recorded as failed runs and are not added
to history.

`usage` is on every result and every `agent_end`. Token counts are what the
provider reported, and zero where it reported none. `requests` counts model
requests that returned a response. For `max_steps` a retried request is one
step, and exactly `max_steps` requests are allowed. A compaction summary
request is counted in the run that needed it. After `resume`, usage and limits
cover the resumed part only. `session.usage` is the last run's.

A model request that fails with HTTP 429, 500, 502, 503 or 504, or cannot
connect, is tried again: four attempts in all, waiting 1, 2, then 4 seconds, or
the response's `Retry-After` seconds up to 30. Nothing is retried once any of
the response has arrived, and cancelling stops the wait at once. Other errors,
including 401 and the provider's own `timeout`, are not retried. Pass
`Agent(sleep=lambda seconds, cancellation: ...)` to control the wait in tests.
A provider of your own opts in by raising `ProviderError(..., retryable=True)`.

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

`run` and `stream` take per-run options, on a session or an agent:

| Option | |
| --- | --- |
| `message_id="..."` | Your ID for this input (sessions only). See [Sending an input once](#sending-an-input-once). |
| `context=value` | Any host value. Handlers and hooks receive it as `context.context`. It is never written to the journal or sent to the model. |
| `instructions="..."`, `model="..."` | Replace the agent's for this run. |
| `extra={...}` | Merged into the request body over the provider's `extra`. |
| `output_schema={...}` | Ask for a typed result. See [Typed output](#typed-output). |
| `trace=True` | Override the agent's `trace` setting for this run. |
| `max_steps`, `max_seconds`, `max_tokens` | Replace the agent's limits for this run. |

Options are checked before the run starts: a wrong type, an unknown option, a
limit that is not a positive finite number (`max_seconds` at most 86,400), or
a prompt nested deeper than 32 levels raises `ValueError` and starts nothing.
`model`, `instructions`, `extra`, `output_schema` and the limits are stored in
the run's `run_start` record and apply again when the run is resumed. `context`
is never stored: pass it to `resume` again.

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
| `agent_start` | `resumed: true` when it continues an open run |
| `text_delta` | `delta` |
| `tool_execution_start` | `toolCallId`, `toolName`, `arguments`, `executionId`; on resume, `rerun: true` when an idempotent tool runs again, `interrupted: true` when the call is settled without running, or `resumed: true` when a paused call is answered |
| `tool_execution_end` | `toolCallId`, `toolName`, `output`, `isError`; `paused: true` (with empty output) when the call paused the run |
| `compaction_start` | |
| `compaction_end` | `status` (`completed`, `failed`, `aborted`), then `changed` or `error` |
| `agent_end` | `status` (`completed`, `failed`, `aborted`, `paused`), `usage`, then `text`, `error`, `pause` or `value` |

For a repeated `message_id`, `agent_start` and `agent_end` both carry
`repeated: true`. Every `tool_execution_end` follows a `tool_execution_start`
for the same call in the same stream.

In a session, every event that has a journal record (`agent_start`,
`tool_execution_start`, `tool_execution_end`, `agent_end`, and a
`compaction_end` that installed a summary) also carries `cursor`, that
record's sequence. Text and reasoning deltas carry none.

`agent_end` reaches a `stream` or `on_event` consumer just before the record
that ends the run is written, so that the consumer can still cancel. If that
write then fails, or the consumer cancels, the journal says `failed` or
`aborted` where the event said `completed`. The RPC and HTTP adapters hold the
event until the write is done and always report what the journal says.

If your callback raises, the run stops and you get your own exception back.

`Agent(trace=True)` adds three more events, for logs and tracing:
`model_request_start` (`attempt`), `model_request_end` (`usage` with
`inputTokens` and `outputTokens`, or `error` and `willRetry`), and
`reasoning_delta` (`delta`) where the provider streams reasoning. They are off
by default so that the event stream of version 1 is unchanged. Every
`model_request_start` is followed by a `model_request_end`, also when the
request fails or is cancelled (`error.code` is then `aborted`).

### Reading events again

`session.events(after=0, limit=100)` rebuilds events from the journal, for
every run: committed, failed, cancelled, paused, or still open. A failed run
ends with an `agent_end` carrying its real `status` and `error`, so a client
that polls can learn of it. It returns
`{"events": [...], "nextAfter": cursor or None}`. Every event has a `cursor`;
pass the last one you handled as `after` to get only what follows. A page is
also bounded in bytes, so it always fits one RPC line or HTTP body. Text comes
back as one `text_delta` per assistant message, because live deltas are not
stored.

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

`Session.list("sessions")` returns the saved IDs. If the agent's model,
instructions or tool definitions differ from the last time the session was
used, that is allowed, and a `configuration` record is added to the journal so
the change is visible in `session.records`.

A session on disk has one writer. Opening it a second time, in this process or another,
raises `SessionBusyError`. The lock is released when the session is closed or
its process exits. Closing never deletes anything.

`session.history` is the committed conversation. `session.records` is
everything in the journal, including runs that did not finish.
`session.inspect(offset=0, limit=20, view="history")` reads either in pages.

### After a crash

If the process dies in the middle of a run, the run is left open in the
journal. After reopening, `session.resumable` is `True`:

```python
session = Session.open(agent, "sessions", session_id)
if session.resumable:
    result = session.resume(on_event=show)
```

`resume` continues where the journal stops, in the same loop as any other run:

- A tool call that finished is kept. It is never run again.
- A tool call that had not started runs now.
- A tool call that started and did not finish is run again, with the same
  `execution_id`, only if the tool is `idempotent=True` both in the journal
  and in the agent you reopened with.
- Otherwise it is not run again. The model receives the tool error "This tool
  call was interrupted and may have partly run. Check its effects before
  continuing." and the run goes on. The stream shows a `tool_execution_start`
  with `interrupted: true`, then the end with that error.
- If the last thing recorded is the final answer, the run is committed without
  asking the model again.

While a run is open, `run` raises `ResumeRequiredError`. `resume` on a session
with no open run raises `NothingToResumeError`.

`session.abandon(reason=None)` ends an open or paused run without resuming it.
The run is recorded as failed with error code `abandoned`, a tool call it left
unfinished is recorded as interrupted, and the next prompt is accepted. With
nothing open it raises `NothingToResumeError`.

`Session(..., on_uncertain="reconcile")` is stricter about the fourth case.
The session then reports `recovery_required`, and `run`, `resume` and
`compact` raise `RecoveryRequiredError` until you look and say what you found:

```python
if session.recovery_required:
    print(session.records[-3:])
    session.reconcile("The email was sent. Do not send it again.")
```

The note is added to history as a user message, and the open run is dropped.

A run that failed or was cancelled in a live process is closed, not open, and
never needs reconciling. A tool call it left unfinished is recorded as
interrupted, nothing of the run enters history, and the next prompt is
accepted. `recovery_required` is true in one case only: an open run with an
interrupted, non-idempotent call, in a session with `on_uncertain="reconcile"`.

### Sending an input once

Give each input your own ID when a client may send it twice:

```python
result = session.run("Charge the card.", message_id="order-7781")
```

If a run with that `message_id` already committed, you get its stored result
(`text`, `value` and `usage`) with `result.repeated == True`, and no model or
tool is called. `result.output` is then empty, and the two events carry the
stored run's cursors. If that run is open, it
is resumed. If it failed, it runs again. If it is running now,
`SessionBusyError`.

### Pausing for an answer

A handler, or `before_tool`, can stop the run and wait for someone else:

```python
from tinyagent import pause

def ask_user(arguments, context):
    return pause({"question": arguments["question"]})
```

The run ends with `result.status == "paused"` and `result.pause`
(`toolCallId`, `toolName`, `payload`); `agent_end` carries the same. The run
stays open in the journal. Later, in this process or another,
`session.paused` returns the same dict and you supply the tool's result:

```python
session.resume(result="Blue.")            # or session.resume(error="No answer.")
```

`result` may be any JSON value, `None` included; `error` must be a string.
Bad input, including a result over the tool output limit, raises `ValueError`
and leaves the pause as it was. The handler and
`before_tool` are not called again; your value is the tool result (subject to
that limit), and `after_tool` sees it. The paused
call's stream ends with a `tool_execution_end` marked `paused: true`; the
resumed stream starts it again with `resumed: true`. This covers
approval over HTTP, questions to the user, tools executed by the client, and
waiting for a webhook. A paused call is never treated as interrupted.

### Storage

A session writes through a `Storage`. Two are included:

```python
from tinyagent import FileStorage, MemoryStorage, Session

Session(agent)                                   # MemoryStorage, private to this session
Session(agent, directory="sessions")             # shorthand for the next line
Session(agent, storage=FileStorage("sessions"))
Session.open(agent, storage, session_id)         # a Storage or a directory
```

To keep sessions somewhere else, implement four methods:

```python
class Storage(Protocol):
    def load(self, session_id): ...                    # records in order, or None if unknown
    def append(self, session_id, expected, record): ... # returns expected + 1
    def list(self): ...                                 # session IDs
    def release(self, session_id): ...                  # session closed; never delete data
```

A record is a JSON object. `append` must be durable before it returns, and
must succeed only if exactly `expected` records are stored for that session;
otherwise it raises `SessionFencedError` and writes nothing. That is the whole
concurrency rule: creating a session is `append(id, 0, header)`, and because a
tool's intent is appended before the tool runs, a writer that has lost the
session never executes the tool. `append` must return `expected + 1`. Any
other exception or return value means the outcome is unknown: the run fails
with `StorageError` (`storage_error`; a fenced one with `session_fenced`),
and afterwards every call on that `Session` raises `SessionFailedError`
(`session_failed`, HTTP 409) until the session is reopened.

`check_storage` is the contract as a test. Run it against your store:

```python
import unittest
from tinyagent import check_storage

class MyStorageTests(unittest.TestCase):
    def test_contract(self):
        check_storage(MyStorage(empty_test_database()))
```

`FileStorage` also takes a lock file per session when it first loads or
appends, so a second process fails fast with `SessionBusyError` instead of
being fenced later. Journals are not portable between the TinyAgent languages.

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

`compact()` raises `ResumeRequiredError` while a run is open or paused.

TinyAgent has no tokenizer. `count_tokens` receives the whole request (`model`,
`instructions`, `history`, `tools`) and returns your estimate. The last
`keep_turns` turns are never summarized.

With a budget, a session also compacts by itself before any model request that
would exceed `max_tokens` less `response_reserve`. A summary is saved before it
is used. If the summary request fails, is cancelled, is empty, or does not save
room, nothing changes and the error reaches you. If the request still does not
fit, the run fails with a message saying so; it does not drop history or retry.

## Tool hooks

```python
def before_tool(call, context):        # call.name, call.arguments, call.call_id
    if call.name == "delete_file":
        return "Deleting is not allowed here."

agent = Agent(model="gpt-5.6", tools=tools, before_tool=before_tool, after_tool=after_tool)
```

`before_tool` runs before every tool call, after its arguments are validated.
Return `None` or `True` to allow it, `False` or a string to deny it (the string
is the tool error the model sees), a dict to replace the arguments (validated
again), or `pause(payload)`. `after_tool(call, result, context)` runs for every
call that was settled, failed ones included, but not for a call that
`before_tool` denied, one that paused, or one settled as interrupted. It receives
`{"output": ..., "isError": ...}` and may return a new output string or a dict
of the same shape. If a hook raises, the call gets an error result, so nothing
is left pending, and the run fails with `HookError` (`hook_error`).

## Typed output

```python
schema = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
result = session.run("Where is the office?", output_schema=schema)
result.value  # {"city": "Oslo"}
```

The schema must describe a JSON object. For that run the agent adds a tool
named `final_output` whose parameters are your schema. When the model calls it
with valid arguments the run completes and `result.value` (and
`agent_end.value`) holds them. Other tool calls in the same turn are answered
"not run: the run finished". The value is stored with the call's result, so a
crash before the commit does not lose it. `result.output` is still the provider's raw
output items, as in version 1. Invalid arguments are
an ordinary tool error. If the model answers in text instead, it is told once
to call the tool; a second miss raises `OutputMissingError` (`output_missing`).
No provider-specific structured-output feature is used.

## Images

A prompt, and a tool's return value, may be a list of parts instead of a string:

```python
session.run([
    {"type": "text", "text": "What is in this picture?"},
    {"type": "image", "mediaType": "image/png", "data": base64_text},
    {"type": "image", "url": "https://example.com/cat.png"},
])
```

History, the journal and all three providers carry them. Chat Completions has
no images in tool messages, so there a tool's images follow in a user message.
RPC and HTTP bodies are still limited to 1 MiB, so pass large images by URL.

## Writing a provider

A provider is any object with one method. It is a generator: yield
`ProviderEvent("text_delta", text)` as text arrives, and return one
`ProviderTurn`.

```python
from tinyagent import ProviderEvent, ProviderTurn, ToolCall

class EchoProvider:
    def stream(self, model, instructions, history, tools, cancellation, extra=None):
        cancellation.raise_if_cancelled()
        text = "You said: " + str(history[-1]["content"])
        yield ProviderEvent("text_delta", text)
        assistant = {"role": "assistant", "content": [{"type": "text", "text": text}]}
        return ProviderTurn(text, assistant, [], [], input_tokens=0, output_tokens=0)
```

`history` is a list of `{"role": "user", "content": ...}`, assistant messages
like the one above, and `{"role": "tool_result", "callId", "toolName",
"output", "isError"}`. To call tools, return `ToolCall(call_id, name,
arguments)` objects as the fourth field and put matching `{"type":
"tool_call", "callId", "name", "arguments"}` blocks in the assistant content;
resume reads the calls back from those blocks. `extra` is only passed when a
run supplies it. [`tinyagent_chat.py`](tinyagent_chat.py) is a complete
provider in about 200 lines.

## Testing your agent

`ScriptedProvider` replays a script instead of calling a model:

```python
from tinyagent import Agent, ScriptedProvider

provider = ScriptedProvider([
    [{"name": "read_notes", "arguments": {"topic": "release"}}],   # a turn of tool calls
    "Two items are open.",                                          # a turn of text
])
result = Agent(model="test", provider=provider, tools=[notes]).run("What is left?")
assert provider.requests[1]["history"][-1]["output"].startswith("Still open")
provider.assert_done()
```

A turn may also be an exception to raise, or a dict with `text`, `calls`,
`reasoning`, `input_tokens` and `output_tokens`. `provider.requests` holds
each request's `model`, `instructions`, `history`, tool names and `extra`.

## Do it yourself

TinyAgent leaves these out on purpose. Each is a few lines on top of it.

**An agent as a tool.** A handler can run another agent:

```python
def research(arguments, context):
    return researcher.run(arguments["question"], cancellation=context.cancellation).text
```

**MCP tools.** List the server's tools with your MCP client and map each one:

```python
tools = [Tool(t.name, t.description, t.inputSchema,
              lambda arguments, context, name=t.name: client.call_tool(name, arguments))
         for t in client.list_tools()]
```

**OpenTelemetry spans.** With `trace=True`, start a span on `agent_start`,
`model_request_start` and `tool_execution_start`, and end it on the matching
`agent_end`, `model_request_end` and `tool_execution_end` in your `on_event`.

**A cost estimate.** `result.usage` has the token counts; multiply by your
prices: `usage["inputTokens"] * input_price + usage["outputTokens"] * output_price`.

## The complete example

[`examples/quickstart.py`](examples/quickstart.py) does all of the above in one
script: it registers a tool, streams a run, cancels a run, exits, reopens the
session in a second process, compacts, and continues.

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
tinyagent -p "fix the failing test" --yolo   # one task, then exit
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
interactive prompt, `/history` prints `session.records`, and `/compact` and
`/reconcile <note>` call the session methods of the same name. A session
reopened with `--resume` that has an open run continues it first, in both
interactive and one-shot mode. `/resume` continues an open run (`/resume
<text>` answers a paused one), and `/abandon [reason]` drops it.
`TINYAGENT_PROVIDER` is `openai`, `anthropic` or `chat`, and
`TINYAGENT_BASE_URL` sets the provider's API root. With `chat` (Chat
Completions) `TINYAGENT_MODEL` is required, because there is no model to
assume, and `OPENAI_API_KEY` is optional.

`-p "task"` (or `--prompt`) runs one session run, prints the text to stdout,
and exits 0 if the run completed and 1 otherwise. `--max-steps`,
`--max-seconds` and `--max-tokens` set the run limits
(`TINYAGENT_MAX_STEPS`, `TINYAGENT_MAX_SECONDS`, `TINYAGENT_MAX_TOKENS`), and
`--cwd` sets the directory Bash commands run in. Only the CLI reads
environment variables; the SDK does not.

### The Bash tool and approval

The CLI's Bash tool runs commands the model writes. Its list of denied commands
catches obvious mistakes and applies in every mode. It is not a sandbox.

The interactive CLI asks before each command: `y` runs it, `n` rejects it,
anything else you type rejects it with that text as the reason, and `a` allows
every command for the rest of the session. Control characters in the command
are shown escaped, so what you approve is what runs. A rejection goes back to the model
as the tool error `Rejected by user: <reason>`. `--allow <regex>` (repeatable)
runs a command without asking only when the pattern matches the whole command,
and never when the command contains any of `; & | $ < > ( )`, a backtick, a
backslash, or a line break; such a command is asked about as usual. `--yolo` (or `TINYAGENT_YOLO=1`) stops asking.
One-shot mode cannot ask, so without `--yolo` or a matching `--allow` it
rejects every command and says so. RPC and HTTP modes never ask.

The same tool is available to your own agent:

```python
from tinyagent import bash_tool

bash = bash_tool(cwd="/srv/app", env={"CI": "1"}, timeout_seconds=120,
                 approve=lambda command: True if command.startswith("git ") else "Only git is allowed.")
```

`env` is merged over the process environment. `approve(command)` returns `True`
to run the command, or `False` or a reason to reject it; for anything more,
use `before_tool`. When `timeout_seconds` passes, the command's process group
is killed and the tool error has the usual shape: `exit_code: 124`, the stdout
and stderr captured so far, then the line `[timed out after 120 s]` (the CLI
uses 120). Each of stdout and stderr is
kept whole up to 30,000 bytes; beyond that the first and last 15,000 bytes are
kept with `[... N bytes elided ...]` between them. The command is not stopped
for producing a lot of output.

To serve your own agent and tools:

```python
from tinyagent import serve

with serve(agent, port=8080, directory="sessions", budget=budget) as server:
    print(server.url)
    ...  # keep the process alive
```

`serve` binds to loopback. Any other host needs `allow_remote=True` and a
`token`. Because the server can run shell commands, it also checks where a
request comes from, on every route and before the token: a `Host` header that
is not the bound address (or `localhost` or a loopback address when bound to
loopback, or a name in `allowed_hosts=[...]`) gets 421, and any request with
an `Origin` header not listed in `allowed_origins=[...]` (none by default)
gets 403. The CLI reads `TINYAGENT_ALLOWED_HOSTS` and
`TINYAGENT_ALLOWED_ORIGINS`, comma separated. `run_rpc(agent, directory="sessions")` does the same for JSONL over
stdin and stdout. Both take `storage=` in place of `directory=`.
[PROTOCOL.md](../../PROTOCOL.md) describes the version 1 wire formats; the
version 2 additions are listed here.

Session commands over RPC are `compact`, `list_sessions`, `resume_session`,
`get_history`, and `reconcile`, next to `prompt`, `abort`, `new_session`, and
`get_state`. Over HTTP they are `GET /sessions`, `POST /sessions` with an
optional `{"sessionId": ...}` to reopen a saved session, `GET /sessions/:id`
to page through it, and `POST /sessions/:id/compact` and `/reconcile`.
`DELETE /sessions/:id` closes a session and keeps its file.

Version 2 adds, over RPC:

- `prompt` takes `messageId`; its response adds `repeated`.
- `{"type": "resume_run"}` continues the open run and answers `{"runId": ...}`
  before the events; with `"result"` or `"error"` it answers a pause. Without
  an open run the error code is `nothing_to_resume`. A `prompt` while a run is
  open fails with `resume_required`.
- `{"type": "abandon_run", "reason": "..."}` answers `{"abandoned": true}`, or
  `nothing_to_resume`.
- `{"type": "get_events", "after": 0, "limit": 100}` returns what
  `session.events` returns.
- `get_state` adds `resumable`, `paused`, and `usage` of the last run.

And over HTTP:

- `POST /sessions/:id/prompts` takes `messageId`.
- `POST /sessions/:id/resume` streams the continued run; its body may hold
  `result` or `error` for a pause.
- `POST /sessions/:id/abandon`, with an optional `{"reason": "..."}`, returns
  `{"abandoned": true}`, or 409 `nothing_to_resume`.
- `GET /sessions/:id/events?after=0&limit=100` returns the same object as
  `get_events`.
- `GET /sessions/:id` adds `resumable` and `paused`.
- New 409 errors: `resume_required`, `nothing_to_resume`, `session_fenced`.

### Runs that outlive the connection

Over HTTP, a client that disconnects cancels its run. Send `"detach": true` in
the body of `/prompts` or `/resume` to change that: the response still streams
while the client stays, but the run continues without it and stops only for
`POST /sessions/:id/cancel`, `DELETE`, or server shutdown.

`GET /sessions/:id/events?after=N&follow=1` is how a client comes back. It
streams JSONL: the stored events after cursor `N`; then, if a run is active,
the text deltas of the turn in progress (kept in memory, up to 4 MiB, and
without a cursor); then the run's live events until its `agent_end`. No event
is sent twice. With no active run it ends after the stored events. A follower
that stops reading is dropped once 4 MiB is waiting for it. The server never
resumes an open run by itself after a restart: reopen the session and post to
`/resume`. RPC is unchanged: end of input cancels the run.

## Things to know

- Providers and tools are synchronous. Use a thread per session if you need
  several conversations at once; one session runs one thing at a time and
  raises `SessionBusyError` otherwise.
- Tool output and tool errors are limited to 131,072 UTF-8 bytes. Larger output
  becomes a tool error. The Bash tool shortens its own output instead, as
  described above.
- Importing `tinyagent`, and constructing a `Tool`, `Agent`, `Session` or
  storage, reads no environment variable and touches no file, socket, thread
  or process, apart from the storage you pass in. There is no module-level
  mutable state. IDs come from `Agent(new_id=...)`, called as `new_id("s")`
  for a session and `new_id("r")` for a run.
- Text that is not valid Unicode (half of a surrogate pair from a model, a
  tool or a client) is stored and sent as U+FFFD. An API key or header with a
  control character, such as a trailing newline, is refused before the
  request with an error that does not contain it. A provider's error text is
  cut to 500 characters.
- `session.close()`, `abandon`, `run`, `resume` and `compact` called from
  inside the session's own handler, hook or event callback raise
  `SessionBusyError` at once. `close()` from another thread cancels the run
  and waits at most three seconds for it.
- A Bash command that leaves a detached child holding its output pipes returns
  one second after the shell exits, with what was captured by then. Output
  that is not UTF-8 is decoded first and shortened second.
- CI runs the tests on Linux and macOS. Windows is not tested.

## Tests

From this directory, with the package installed:

```sh
python -m unittest discover -p 'test_*.py'
python tiny_agent.py --self-test
```

The tests need no API key and no network beyond loopback. They read the shared
fixtures in `../../conformance`, and write only to the system temp directory.
