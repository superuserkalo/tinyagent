# Contributing to TinyAgent

Thanks for helping. TinyAgent is deliberately small, and most of the work of
contributing is keeping it that way.

## Before you start

- For a bug, open an issue with the language, the version or commit, and the
  smallest steps that reproduce it.
- For a new feature, open an issue first. The [README](README.md#what-is-deliberately-left-out)
  lists what is intentionally out of scope, and a feature has to earn its lines.
- For a typo or a small fix, just send the pull request.

## Which code is supported

| Implementation | Status |
|---|---|
| Python, TypeScript | Supported, at protocol version 2. Covered by CI. [STATUS.md](STATUS.md) records what is verified. |
| Go, Rust, Zig, C#, Java, PHP, Elixir | Experimental reference implementations, still at protocol version 1 and in memory only. Fixes are welcome. They are not covered by CI and carry no maintenance promise. |

A language moves to "supported" when it implements protocol version 2, passes
the black-box harness and CI, and has someone who will maintain it. Porting one
of the seven is the most useful large contribution; see
[conformance/blackbox/README.md](conformance/blackbox/README.md#adding-a-language).

## Setting up

You need Python 3.10 or newer and Node.js 22.18 or newer.

```sh
git clone https://github.com/superuserkalo/tinyagent.git
cd tinyagent

# Python
python3 -m venv .venv
.venv/bin/python -m pip install -e ./tinyagents/python

# TypeScript
npm ci --ignore-scripts --prefix tinyagents/typescript
```

No API key is needed to build or test. You only need one to talk to a real
model through a CLI.

## Running the checks

```sh
# Python
cd tinyagents/python
../../.venv/bin/python -m unittest discover -p 'test_*.py'

# TypeScript (the library is sdk.ts; tiny_agent.ts is the CLI entry file)
cd tinyagents/typescript
npm run check
npm test

# From the repository root: the shared fixtures, then the black-box harness
.venv/bin/python conformance/run.py python typescript
.venv/bin/python conformance/blackbox/run.py python typescript --quick
.venv/bin/python conformance/blackbox/run.py python typescript          # everything
```

The harness starts each CLI as a process and drives it against a scripted
model server, killing it, cancelling it and injecting provider faults along
the way. The quick run takes about a minute per language and the full run
about two. A failure prints a command that reproduces that one case. Run one
harness at a time: parallel runs on one machine cause timing failures.

Each experimental language has its own build and check instructions in its
`README.md`.

## What a good change looks like

- **It keeps one agent loop per language.** The CLI, RPC mode and HTTP server
  are thin adapters over the same `Agent` and `Session` the SDK exports.
- **It is readable.** People come here to learn how an agent works. Plain code
  beats a clever abstraction, and a new dependency needs a stated reason.
- **Its tests run offline.** Use the existing fake providers and fixtures. No
  network, no API keys.
- **Its tests clean up.** Use the system temp directory. After a test run,
  `git status` must be clean.
- **It changes the protocol everywhere at once.** If you change behaviour that
  `PROTOCOL.md` describes, update that file, the harness scenarios in
  `conformance/blackbox/`, and every version 2 implementation in the same pull
  request. Copy `PROTOCOL.md` to `tinyagents/go/` and `tinyagents/rust/`
  afterwards; the runner rejects stale copies. Leave
  `conformance/rpc_v1.json` alone: three version 1 packages bundle it.
- **A bug fix comes with a regression test** that fails without the fix.
- **It upholds the invariants** numbered in [PROTOCOL.md](PROTOCOL.md#invariants):
  every tool start has one end, a tool's intent is journaled before it runs, a
  non-idempotent tool never runs twice for one call, the journal is the truth
  about how a run ended, nothing waits without a bound, and a session stays
  usable after any failure.
- **Its documentation is exact.** Say what the code does now. Do not describe
  planned work as finished.

## Pull requests

1. Branch from `main`.
2. Keep the change focused. Unrelated clean-ups belong in their own pull request.
3. Run the checks for everything you touched.
4. In the description, say what changed, why, and what you ran to verify it.
   If something is untested or unverified on a platform, say so.

CI runs the Python suite on Python 3.10, 3.12 and 3.14 and the TypeScript suite
on Node 22 and 24, each on Linux and macOS, and on Linux the shared conformance
run and the black-box harness in quick mode.

## Security

TinyAgent's CLIs run shell commands written by a model. Read
[SECURITY.md](SECURITY.md) before running one outside a throwaway directory,
and report vulnerabilities privately as described there.

## Licence

By contributing you agree that your contribution is licensed under the
[MIT licence](LICENSE).
