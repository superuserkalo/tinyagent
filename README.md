# TinyAgent

[![CI](https://github.com/superuserkalo/tinyagent/actions/workflows/ci.yml/badge.svg)](https://github.com/superuserkalo/tinyagent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A small agent SDK you can read in an afternoon, written natively in several
languages with no agent framework underneath.

Each implementation calls the model API directly, runs your tools, streams what
happens, and repeats until the model answers:

```text
user -> model -> tool call -> observation -> model -> ... -> answer
```

That loop is the agent. Everything else is HTTP, JSON, process handling, and
storage in the language you chose.

## What you get

- **Three concepts.** A `Tool` is a function with a JSON Schema. An `Agent` is a
  model, instructions and tools. A `Session` is one conversation.
- **Three providers.** OpenAI Responses, Anthropic Messages and
  OpenAI-compatible Chat Completions, all streaming, called directly. Each takes
  a base URL, extra headers and extra request fields, so gateways and
  self-hosted servers work. Writing your own provider is one method.
- **A storage interface.** A session writes every step through four operations:
  load, append, list, release. A JSONL file store and a memory store are
  included, and the contract is exported as a test for your own store.
- **Resume.** A tool call is recorded before it runs and after. If the process
  dies mid-run, a new process reopens the session and continues: finished calls
  are kept, calls marked idempotent run again with the same execution ID, and
  for any other interrupted call the model is told it may have partly run. It
  is never run twice on its own.
- **Pause.** A tool or a hook can stop a run and wait for a value: an approval,
  an answer from the user, a webhook. Any process can supply it later.
- **Typed output.** Give a run a JSON Schema and get a validated object back.
- **Hooks.** `beforeTool` can allow, deny, rewrite or pause a call. `afterTool`
  can replace a result.
- **Limits and retries.** Steps, seconds and tokens per run. Provider requests
  that fail before any response arrives are retried with backoff.
- **Images.** Prompts and tool results may carry images, inline or by URL.
- **Compaction.** Older history is summarised when the context fills up. The
  raw history is kept.
- **Events you can replay.** Every run streams ordered events, and a session
  can rebuild them from its journal, so a client can disconnect and come back.
- **One loop, four ways in.** The imported SDK, a coding CLI (interactive, or
  one-shot with `-p`), a JSONL RPC mode, and an HTTP server all use the same
  `Agent` and `Session`. The CLI asks before it runs a command.

All of this is protocol version 2, which Python and TypeScript implement. The
other seven languages are still at version 1: the loop, two providers, events,
RPC and HTTP, with sessions in memory.

## Status

| Implementation | Protocol | Status |
|---|---|---|
| [Python](tinyagents/python/README.md) | 2 | Supported. 279 unit tests, verified on Python 3.10 and 3.14. Passes the black-box harness. |
| [TypeScript](tinyagents/typescript/README.md) | 2 | Supported. 214 unit tests, verified on Node.js 22.18, 24 and 26. Passes the black-box harness. |
| [Go](tinyagents/go/README.md), [Rust](tinyagents/rust/README.md), [Zig](tinyagents/zig/README.md), [C#](tinyagents/csharp/README.md), [Java](tinyagents/java/README.md), [PHP](tinyagents/php/README.md), [Elixir](tinyagents/elixir/README.md) | 1 | Experimental reference implementations, in memory only. Not in CI, last verified on macOS in September 2026. |

Version 0.0.1. Nothing is published to PyPI or npm yet; install from a clone.
Windows is untested. [STATUS.md](STATUS.md) says what is verified, what is
not, and what comes next.

## Quick start: Python

Python 3.10 or newer.

```sh
git clone https://github.com/superuserkalo/tinyagent.git
cd tinyagent
python3 -m venv .venv
.venv/bin/python -m pip install ./tinyagents/python
export OPENAI_API_KEY="..."
```

```python
from tinyagent import Agent, Session, Tool

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

with Session(agent, directory="sessions") as session:
    result = session.run("What is left to do?")
    print(result.text)
    print(session.id)  # keep this to resume later
```

The [Python guide](tinyagents/python/README.md) covers streaming, cancellation,
storage, resume, pause, hooks, typed output, images and compaction, and
[`examples/quickstart.py`](tinyagents/python/examples/quickstart.py) runs a
tool, streams, cancels, restarts, resumes and compacts in one file.

## Quick start: TypeScript

Node.js 22.18 or newer, which runs `.ts` files directly.

```sh
git clone https://github.com/superuserkalo/tinyagent.git
cd tinyagent/tinyagents/typescript
npm ci --ignore-scripts
npm pack                      # builds dist/ and writes tiny-agent-sdk-0.0.1.tgz

cd /path/to/your/project      # a project with "type": "module"
npm install /path/to/tinyagent/tinyagents/typescript/tiny-agent-sdk-0.0.1.tgz
export OPENAI_API_KEY="..."
```

```typescript
import { Agent, Tool } from "tiny-agent-sdk";

const forecast = new Tool({
  name: "forecast",
  description: "Return tomorrow's forecast for a city.",
  parameters: {
    type: "object",
    properties: { city: { type: "string" } },
    required: ["city"],
    additionalProperties: false,
  },
  handler: ({ city }) => ({ city, sky: "clear", highCelsius: 24 }),
});

const agent = new Agent({
  model: "gpt-5.6",
  instructions: "Answer weather questions with the forecast tool.",
  tools: [forecast],
});

const result = await agent.run("What is the forecast for Lisbon?");
console.log(result.text);
```

The [TypeScript guide](tinyagents/typescript/README.md) covers sessions,
streaming, cancellation, storage, resume, pause, hooks, typed output, images
and compaction, and
[`examples/session.ts`](tinyagents/typescript/examples/session.ts) runs a tool,
streams, cancels, restarts, resumes and compacts in one file.

## The coding CLI

Each implementation ships a small coding agent with one `bash` tool. From a
clone, after the install steps above:

```sh
.venv/bin/python tinyagents/python/tiny_agent.py
node tinyagents/typescript/tiny_agent.ts
```

Type a task at the prompt. Ctrl-C exits. In Python and TypeScript:

- **It asks before each command.** Answer `y` to run it, `n` to reject it, `a`
  to stop asking for the rest of the session, or type a reason and the model is
  told why you refused.
- `--yolo` (or `TINYAGENT_YOLO=1`) runs commands without asking.
- `--allow <regex>` runs a command without asking when the pattern matches the
  whole command and the command does not chain, substitute or redirect.
- `-p "task"` runs one task, prints the answer and exits 0 if the run
  completed, 1 otherwise. It cannot ask, so it needs `--yolo` or `--allow`;
  without either every command is rejected.
- `--resume <session id>` reopens a saved session and continues a run that was
  interrupted. `--list-sessions` prints the saved IDs.
- `--max-steps`, `--max-seconds`, `--max-tokens` and `--cwd` bound and place a run.

```sh
node tinyagents/typescript/tiny_agent.ts -p "run the tests and summarise failures" --yolo
```

To use another provider:

```sh
export TINYAGENT_PROVIDER="anthropic"        # or "chat" for a Chat Completions server
export ANTHROPIC_API_KEY="..."
export TINYAGENT_MODEL="claude-sonnet-4-6"
export TINYAGENT_BASE_URL="http://localhost:11434/v1"   # optional: a gateway or local server
```

The seven version 1 CLIs do not ask for approval and have none of these flags
or the `chat` provider. Of them, Go, Rust and Zig can use Anthropic.

**The CLI runs shell commands written by a model.** It rejects a short list of
obviously destructive ones (`rm`, `dd`, `sudo`, `git reset --hard` and similar)
and reports the rejection to the model as a tool error with exit code 126. That
list is a guardrail against accidents, not a sandbox, and approval is only as
good as your reading of the command. Run the CLI only in a directory where
model-written commands are acceptable, and use a disposable container or
virtual machine for anything you do not trust. See [SECURITY.md](SECURITY.md).

## RPC and HTTP

The same agent is available to other programs in two ways.

```sh
node tinyagents/typescript/tiny_agent.ts --mode rpc     # JSONL on stdin and stdout
node tinyagents/typescript/tiny_agent.ts serve          # HTTP on 127.0.0.1:8080
```

RPC mode reads one JSON command per line and writes responses and events the
same way: `prompt`, `resume_run`, `abandon_run`, `abort`, `get_state`,
`get_events`, `get_history`, `compact`, `reconcile`, `new_session`,
`list_sessions` and `resume_session`.

The HTTP server streams a run's events as JSONL. A prompt sent with
`"detach": true` keeps running when the client disconnects, and
`GET /sessions/:id/events?after=N&follow=1` picks the stream up again. The
server binds to loopback by default; binding anywhere else requires both an
explicit opt-in and a bearer token. It refuses any request whose `Host` header
does not name it and, unless you list allowed origins, any request that carries
an `Origin` header, so a web page cannot reach it. Neither mode asks for
approval: they run with the blacklist only.

[PROTOCOL.md](PROTOCOL.md) is the full contract for version 2: the loop's
rules, the journal, resume, events, commands, routes, error codes, limits and
the invariants every implementation must uphold.

## Repository layout

```text
tinyagents/<language>/   one native implementation each
PROTOCOL.md              the contract, protocol version 2
conformance/             shared fixtures, the runner, and the black-box harness
sdk/README.md            reference for the version 1 in-memory core across languages
docs/                    notes on individual implementations
STATUS.md                what is verified, known limits, what comes next
```

## How it is tested

No API key is needed. Every test uses a local fake provider.

- **Unit suites.** 279 tests in Python and 214 in TypeScript, including the
  storage contract run against both built-in stores, crash and resume at each
  point in a run, pause, hooks, typed output and all three providers.
- **Shared conformance.** Both languages read the same fixtures in
  [`conformance/`](conformance/README.md): RPC scenarios and limits, tool
  argument cases and provider SSE transcripts.
- **The black-box harness.** [`conformance/blackbox/`](conformance/blackbox/README.md)
  starts a language's CLI as a process and drives it over RPC, HTTP and
  one-shot mode against a scripted model server. Besides its scenarios it runs
  fault sweeps: it kills the process at every journal record and resumes or
  abandons, injects each provider fault at each request, cancels after each
  event, disconnects the client after each event, and takes seeded random
  walks. Eight invariants are checked after every step. It runs 515 cases per
  language; both pass, with zero divergences between them.
- **Adversarial review.** The version 2 code was reviewed with the aim of
  breaking it. Every reproduction it produced is now a regression test.

```sh
# Python
cd tinyagents/python && ../../.venv/bin/python -m unittest discover -p 'test_*.py'

# TypeScript
cd tinyagents/typescript && npm run check && npm test

# From the repository root: shared fixtures, then the harness
.venv/bin/python conformance/run.py python typescript
.venv/bin/python conformance/blackbox/run.py python typescript --quick
```

Drop `--quick` for the full harness run.

## What is deliberately left out

Parallel tool calls, an MCP client, handoff or orchestration primitives, graphs
or workflow engines, memory or retrieval, a guardrail framework, an
OpenTelemetry dependency, dollar costs, a model registry, UI stream protocols,
built-in file or web tools, prompt queues and steering, built-in TLS, and
multi-user ownership. TinyAgent stays small on purpose. The Python and
TypeScript guides show how to do the common ones yourself in a few lines: an
agent as a tool, MCP tools mapped to `Tool`, OpenTelemetry spans from events,
and a cost estimate from usage.

## Contributing

Bug reports and fixes are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md)
first. Coding agents working in this repository should read
[AGENTS.md](AGENTS.md).

## Licence

[MIT](LICENSE).
