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
- **Two providers.** OpenAI Responses and Anthropic Messages, both streaming,
  called directly.
- **Durable sessions.** A session is saved to disk as it runs. A new process can
  reopen it, inspect it, and continue.
- **Compaction.** Older history is summarised when the context fills up. The
  raw history is kept.
- **Honest recovery.** If a process dies while a tool is running, the session
  says so and waits for you. It never runs the tool again on its own.
- **One loop, four ways in.** The imported SDK, a coding CLI, a JSONL RPC mode,
  and an HTTP server all use the same `Agent` and `Session`.

Durable sessions, compaction and recovery are in Python and TypeScript. The
other languages keep sessions in memory.

## Status

| Implementation | Status |
|---|---|
| [Python](tinyagents/python/README.md) | Supported. Tested in CI on Python 3.10, 3.12 and 3.14, Linux and macOS. |
| [TypeScript](tinyagents/typescript/README.md) | Supported. Tested in CI on Node.js 22 and 24, Linux and macOS. |
| [Go](tinyagents/go/README.md), [Rust](tinyagents/rust/README.md), [Zig](tinyagents/zig/README.md), [C#](tinyagents/csharp/README.md), [Java](tinyagents/java/README.md), [PHP](tinyagents/php/README.md), [Elixir](tinyagents/elixir/README.md) | Experimental reference implementations. Not in CI, last verified on macOS in September 2026. |

Version 0.0.1. Nothing is published to PyPI or npm yet; install from a clone.
Windows is untested.

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
resume, compaction and recovery, and
[`examples/quickstart.py`](tinyagents/python/examples/quickstart.py) does all of
them in one file.

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
streaming, cancellation, resume, compaction and recovery, and
[`examples/session.ts`](tinyagents/typescript/examples/session.ts) does all of
them in one file.

## The coding CLI

Each implementation ships a small coding agent with one `bash` tool. From a
clone, after the install steps above:

```sh
.venv/bin/python tinyagents/python/tiny_agent.py
node tinyagents/typescript/tiny_agent.ts
```

Type a task at the prompt. Ctrl-C exits. To use Anthropic instead of OpenAI:

```sh
export TINYAGENT_PROVIDER="anthropic"
export ANTHROPIC_API_KEY="..."
export TINYAGENT_MODEL="claude-sonnet-4-6"
```

**The CLI runs shell commands written by a model.** It rejects a short list of
obviously destructive ones (`rm`, `dd`, `sudo`, `git reset --hard` and similar)
and reports the rejection to the model as exit code 126. That list is a
guardrail against accidents, not a sandbox. Run the CLI only in a directory
where model-written commands are acceptable, and use a disposable container or
virtual machine for anything you do not trust. See [SECURITY.md](SECURITY.md).

## RPC and HTTP

The same agent is available to other programs in two ways.

```sh
node tinyagents/typescript/tiny_agent.ts --mode rpc     # JSONL on stdin and stdout
node tinyagents/typescript/tiny_agent.ts serve          # HTTP on 127.0.0.1:8080
```

RPC mode reads one JSON command per line and writes responses and events the
same way. The HTTP server streams a run's events as JSONL. It binds to loopback
by default, and binding anywhere else requires both an explicit opt-in and a
bearer token.

[PROTOCOL.md](PROTOCOL.md) is the full contract: commands, routes, events, error
codes, limits, and the session and recovery rules.

## Repository layout

```text
tinyagents/<language>/   one native implementation each
PROTOCOL.md              the contract every implementation follows
conformance/             fixtures shared by all languages, and the runner
sdk/README.md            SDK reference across languages
docs/                    implementation notes and the acceptance audit
FINISH_CONTRACT.md       what the first release requires, and where it stands
ROADMAP.md               scope, milestones, and what is deliberately left out
```

## Running the checks

No API key is needed. Tests use local fake providers.

```sh
# Python
cd tinyagents/python && ../../.venv/bin/python -m unittest discover -p 'test_*.py'

# TypeScript
cd tinyagents/typescript && npm run check && npm test

# Fixtures shared by both, from the repository root
.venv/bin/python conformance/run.py python typescript
```

## What is deliberately left out

Branching, orchestration, plugins, parallel tools, steering, prompt queues, an
approval interface, extra storage backends, built-in TLS, and multi-user
ownership. TinyAgent stays small on purpose. The [roadmap](ROADMAP.md) explains
what would bring any of them in.

## Contributing

Bug reports and fixes are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md)
first. Coding agents working in this repository should read
[AGENTS.md](AGENTS.md).

## Licence

[MIT](LICENSE).
