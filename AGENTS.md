# Working in this repository

Instructions for coding agents. People should read [CONTRIBUTING.md](CONTRIBUTING.md),
which says the same things at more length.

## What this is

TinyAgent is a small agent SDK written natively in several languages, with no
agent framework underneath. Each implementation has one handwritten agent loop.
An imported SDK, a coding CLI, a JSONL RPC mode, and an HTTP server all use that
one loop.

- `tinyagents/python/` and `tinyagents/typescript/` are the supported
  implementations. CI runs them and the release contract applies to them.
- `tinyagents/{go,rust,zig,csharp,java,php,elixir}/` are experimental
  reference implementations. Keep them building, but do not expand them unless
  the task says to.
- `PROTOCOL.md` is the contract every implementation follows.
- `conformance/` holds the fixtures all languages share, and the runner.
- `FINISH_CONTRACT.md` and `ROADMAP.md` define what the first release is.

## Commands

Python, from `tinyagents/python/`:

```sh
python -m pip install -e .
python -m unittest discover -p 'test_*.py'
python tiny_agent.py --self-test
```

TypeScript, from `tinyagents/typescript/` (Node 22.18 or newer, which runs the
`.ts` files directly):

```sh
npm ci --ignore-scripts
npm run check
npm test
node tiny_agent.ts --self-test
```

Shared conformance, from the repository root:

```sh
python conformance/run.py python typescript
```

Run the checks for every implementation you touched before you say a change is
done, and report the real result.

## Rules

- **One loop per language.** The CLI, RPC and HTTP adapters call the same
  `Agent` and `Session` as the imported SDK. Never add a second model or tool
  loop inside an adapter.
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
- **Protocol changes go in three places together:** `PROTOCOL.md`, the fixtures
  in `conformance/`, and every supported implementation. Go, Rust and Zig bundle
  byte-identical copies of the fixtures; the runner rejects stale copies, so
  refresh them with the commands in each package's README.
- **Imports have no side effects.** Importing the SDK must not read the
  environment, start a server, or print.
- **Be exact in documentation.** Describe what the code does today. Do not
  describe an unfinished feature as shipped, and do not copy old test counts.

## Safety

The CLIs execute model-written shell commands. The destructive-command
blacklist is a guardrail, not a sandbox. Do not present it as a security
boundary in code comments or documentation. See [SECURITY.md](SECURITY.md).

## Out of scope for now

Branching, orchestration, plugins, predictive compaction, queues, steering, an
approval interface, extra storage backends, TLS termination and multi-user
ownership. See the roadmap before proposing any of them.
