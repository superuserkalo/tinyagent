# Working in this repository

Instructions for coding agents. People should read [CONTRIBUTING.md](CONTRIBUTING.md),
which says the same things at more length.

## What this is

TinyAgent is a small agent SDK written natively in several languages, with no
agent framework underneath. Each implementation has one handwritten agent loop.
An imported SDK, a coding CLI, a JSONL RPC mode, and an HTTP server all use that
one loop.

- `tinyagents/python/` and `tinyagents/typescript/` are the supported
  implementations, at protocol version 2. CI runs them.
- `tinyagents/{go,rust,zig,csharp,java,php,elixir}/` are experimental
  reference implementations, still at protocol version 1 and in memory only.
  Keep them building, but do not expand them unless the task says to.
- `PROTOCOL.md` is the contract, protocol version 2.
- `conformance/` holds the fixtures all languages share, the runner, and the
  black-box harness in `conformance/blackbox/`.
- `STATUS.md` says what is verified, the known limits and what comes next.

## Commands

Python, from `tinyagents/python/`:

```sh
python -m pip install -e .
python -m unittest discover -p 'test_*.py'
python tiny_agent.py --self-test
```

TypeScript, from `tinyagents/typescript/` (Node 22.18 or newer, which runs the
`.ts` files directly). The library is `sdk.ts`; `tiny_agent.ts` is the CLI
entry file and re-exports it:

```sh
npm ci --ignore-scripts
npm run check
npm test
node tiny_agent.ts --self-test
```

Shared conformance and the black-box harness, from the repository root:

```sh
python conformance/run.py python typescript
python conformance/blackbox/run.py python typescript --quick    # about a minute per language
python conformance/blackbox/run.py python typescript            # everything
python conformance/blackbox/run.py python --only 'crash[rpc,k=7,resume]' --seed 1   # one case
```

Run the checks for every implementation you touched before you say a change is
done, and report the real result. Do not run two harness runs at once on one
machine: the load produces timing failures.

## Rules

- **One loop per language.** The CLI, RPC and HTTP adapters call the same
  `Agent` and `Session` as the imported SDK. Never add a second model or tool
  loop inside an adapter. Resume and pause go through that loop too.
- **Keep it small and readable.** This code is meant to be read by people
  learning how agents work. Prefer plain code over abstraction. Do not add a
  dependency without a reason you can state in the pull request.
- **Tests run offline.** No network and no API keys. Use the fake providers and
  fixtures that already exist.
- **Tests use the system temp directory** and remove what they create. A test
  run must leave `git status` clean. Never write session files into the
  repository.
- **Do not weaken a test to make it pass.** If a test is wrong, say why in the
  pull request and fix the test deliberately.
- **A bug fix comes with a regression test** that fails without the fix.
- **Protocol changes go in three places together:** `PROTOCOL.md`, the harness
  scenarios in `conformance/blackbox/`, and every version 2 implementation.
  After changing `PROTOCOL.md`, copy it to `tinyagents/go/PROTOCOL.md` and
  `tinyagents/rust/PROTOCOL.md`; the runner rejects stale copies.
- **Do not change `conformance/rpc_v1.json`.** Go, Rust and Zig bundle
  byte-identical copies and are still at version 1. The version 2 suites
  overlay the new `get_state` fields on it in their tests.
- **Uphold the invariants** numbered in `PROTOCOL.md`: well-formed streams and
  provider requests, at most one effect per tool call, intent before
  execution, the journal as the truth, bounded waits, stable replay, a session
  that stays usable, and no process left running.
- **Imports have no side effects.** Importing the SDK, or constructing an
  Agent, Tool, Session or storage, must not read the environment, touch a
  file, start a server, or print. Only the CLI entry reads the environment.
- **Be exact in documentation.** Describe what the code does today. Do not
  describe an unported language as having version 2 features, and do not copy
  old test counts.

## Safety

The CLIs execute model-written shell commands. The destructive-command
blacklist is a guardrail, not a sandbox. Do not present it as a security
boundary in code comments or documentation. See [SECURITY.md](SECURITY.md).

## Out of scope

Deliberately not added: parallel tool calls, an MCP client, handoff or
orchestration primitives, graphs or workflow engines, memory or retrieval, a
guardrail framework, an OpenTelemetry dependency, dollar costs, a model
registry, UI stream protocols, built-in file or web tools, prompt queues and
steering, TLS termination and multi-user ownership. Open an issue before
proposing any of them. Approval and storage backends are in scope: approval
goes through `beforeTool` and the bash tool's `approve`, and a storage backend
is an implementation of the four-operation `Storage` interface.
