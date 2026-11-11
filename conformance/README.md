# TinyAgent conformance

This directory holds what all languages share for testing the contract in
[PROTOCOL.md](../PROTOCOL.md):

- language-neutral **fixtures** that every implementation's own tests read;
- `run.py`, the **runner**, which runs each language's native checks;
- `blackbox/`, the **black-box harness**, which drives an implementation from
  outside, as a process, and injects faults.

| Path | What it holds |
| --- | --- |
| `rpc_v1.json` | Protocol version 1 RPC limits and scenarios |
| `tool_arguments.json` | JSON Schema cases for tool argument validation |
| `providers/*.sse` | Synthetic SSE transcripts for OpenAI Responses and Anthropic Messages |
| `run.py` | The runner |
| `blackbox/` | The black-box harness; see [its README](blackbox/README.md) |

When PROTOCOL.md and these disagree, fix them together.

## Fixtures

### `rpc_v1.json`

```json
{"protocolVersion": 1, "limits": {...}, "scenarios": [...]}
```

`limits` are UTF-8 byte counts:

| Key | Value | Counts |
| --- | --- | --- |
| `jsonlLineBytes` | 1,048,576 | One JSONL line before its trailing LF |
| `httpBodyBytes` | 1,048,576 | An HTTP request body |
| `queuedOutputBytes` | 4,194,304 | Complete framed lines waiting to be written, including their LFs |
| `toolOutputBytes` | 131,072 | One serialized tool observation |

A synchronous writer has no in-memory output queue. An asynchronous writer
either applies backpressure within the limit or rejects output that would
exceed it.

Each scenario has a `name`. Scenarios that are a plain exchange carry
`commands` and the exact `expected` responses:

```json
{
  "name": "unknown_with_id",
  "commands": [{"id": "bad", "type": "not_a_command"}],
  "expected": [{"id": "bad", "type": "response", "command": "not_a_command",
    "success": false, "error": {"code": "invalid_command", "message": "Unknown command type"}}]
}
```

Scenarios that need a provider or a running process describe the setup and the
outcome instead, with keys such as `providerScript`, `activeRun`,
`expectedEventTypes`, `expectedTerminalStatus`, `expectedMessageCount` and
`expectedProtocolError`. Each language supplies its own fake provider for them.

The scenarios are `idle_state`, `idle_abort`, `unknown_with_id`,
`unknown_without_id`, `invalid_prompt`, `invalid_id`, `busy_abort_rollback`,
`prompt_completion`, `failed_run_rollback`, `new_session`, `jsonl_framing`,
`clean_eof`, `malformed_json`, `oversized_output`, `oversized_line` and
`invalid_utf8`.

Command framing, response fields, error codes and limits must match this file
exactly.

**This file describes protocol version 1 and must not be changed.** Go, Rust
and Zig bundle byte-identical copies of it and are still at version 1. It
covers the baseline commands only: `prompt`, `abort`, `new_session` and
`get_state`, with `"protocolVersion": 1` and the version 1 `get_state` fields.

Python and TypeScript are at version 2 and still read it. Their tests overlay
what version 2 adds on the expected `get_state` data (`protocolVersion: 2`,
`resumable`, `paused`, `usage`) and on the prompt response (`repeated`), and
check everything else in the file unchanged. Everything else in version 2 is
tested by their own suites and by the black-box harness.

### `tool_arguments.json`

```json
{"dialect": "https://json-schema.org/draft/2020-12/schema", "cases": [...]}
```

Each case has a `name`, a `schema`, a list of `valid` argument objects and a
list of `invalid` ones. A tool built from `schema` must accept every `valid`
object and turn every `invalid` one into a tool error before its handler runs.

The cases are:

- required text and no extra properties
- nested arrays, integer bounds, and enums
- local references and alternatives
- composed properties and unevaluated property rejection
- recursive local dynamic references
- large integral exponent values

These cases supplement each validator's own dialect support. They are not a
complete JSON Schema test suite.

### `providers/`

`openai_text.sse`, `openai_tool.sse`, `anthropic_text.sse` and
`anthropic_tool.sse` are synthetic SSE transcripts. They include a tool call
whose arguments are split across events, non-ASCII text, multiline SSE data,
OpenAI encrypted reasoning items, and Anthropic thinking signatures.

Each language feeds them to its provider parsers, usually split at awkward byte
boundaries, and checks the resulting text, tool calls and replay metadata.

There are no shared transcripts for the Chat Completions provider yet. The
Python and TypeScript tests define their Chat Completions streams inline, and
the harness's fake provider speaks that API in every `[chat]` case.

## Bundled copies

Three packages carry byte-identical copies so their tests run outside this
repository. The runner refuses to run a language whose copies are missing,
changed or extra.

| Package | Copies | Refresh |
| --- | --- | --- |
| Go | `tinyagents/go/conformance/`, `tinyagents/go/PROTOCOL.md` | see [the Go README](../tinyagents/go/README.md) |
| Rust | `tinyagents/rust/conformance/`, `tinyagents/rust/PROTOCOL.md` | see [the Rust README](../tinyagents/rust/README.md) |
| Zig | `tinyagents/zig/test/fixtures/` (the four `.sse` files and both JSON files) | command below |

From the repository root:

```sh
cp conformance/*.json conformance/providers/*.sse tinyagents/zig/test/fixtures/
```

Change the shared file first, then refresh the copies. Never edit a copy.

## Runner

From the repository root:

```sh
python3 conformance/run.py python typescript   # the two supported languages
python3 conformance/run.py go                  # one experimental language
python3 conformance/run.py                     # all nine
```

The runner compares the bundled copies of PROTOCOL.md and the fixtures (see
above) before it runs Go, Rust or Zig. After changing PROTOCOL.md, copy it to
`tinyagents/go/PROTOCOL.md` and `tinyagents/rust/PROTOCOL.md`.

Language names are `python`, `typescript`, `go`, `rust`, `zig`, `java`,
`elixir`, `csharp` and `php`.

The runner uses its own interpreter for the Python tests. If you installed the
Python package into a virtual environment, start the runner with that
environment's interpreter, for example `.venv/bin/python conformance/run.py`.

What it runs, in each language's directory:

| Language | Commands | Needs first |
| --- | --- | --- |
| Python | `python -m unittest discover -p 'test_*.py'`, `tiny_agent.py --self-test` | the package installed |
| TypeScript | `tsc --noEmit`, `tsc -p tsconfig.test.json`, `node --test *.test.ts`, `tiny_agent.ts --self-test` | `npm ci --ignore-scripts` |
| Go | `go test -race -timeout 90s ./...`, `go vet ./...`, `go run ./cmd/tinyagent --self-test` | |
| Rust | `cargo test --locked --no-fail-fast`, `cargo clippy`, the `offline` example, `--self-test` | |
| Zig | `cargo fmt --check` and `cargo clippy` on `schema-native`, `zig fmt --check`, `zig build test example -j1` | Cargo |
| C# | `python check.py` | .NET 8 SDK |
| Java | `python check.py` | `mvn package`, so the dependency JARs are in the local Maven repository |
| PHP | `php check.php` | `composer install --no-scripts --no-plugins` |
| Elixir | `elixir check.exs`, `mix hex.build`, `tiny_agent.exs --self-test` | `mix deps.get` and `mix compile` |

Behavior:

- Provider credentials and the `TINYAGENT_*` provider, model and server
  variables are removed from the child environment. No check calls a real
  provider.
- The checks include tests that bind loopback sockets.
- Each command has a 180-second timeout.
- A language reports `CHECKS PASSED`, `FAILED`, or `INCOMPLETE` with a reason.
  `INCOMPLETE` means a missing runtime or missing dependencies. No language is
  skipped silently.
- The exit status is zero only if every selected language reports
  `CHECKS PASSED`.

## Black-box harness

[`blackbox/`](blackbox/README.md) is one harness that drives any implementation
through its public surfaces only: RPC mode, the HTTP server, one-shot mode and
the interactive prompt. It starts the language's CLI as a process. The model is
a scripted fake server on loopback that speaks the OpenAI Responses, Anthropic
Messages and Chat Completions streaming APIs, records every request, and
misbehaves on demand. The CLI's only tool is bash, so every tool call is a
shell command whose effect is a line appended to a file in the case's temp
directory. It needs Python 3.10 or newer and nothing else, and writes nothing
into the repository.

It has two parts:

- **Scenarios**, one function per specified behaviour.
- **Fault sweeps** over one standard conversation: kill the process at every
  journal record and resume or abandon (`crash`), inject each provider fault at
  each model request (`provider`), cancel after each event (`cancel`),
  disconnect the HTTP client after each event, attached and detached
  (`client`), and seeded random walks over one session (`walk`).

Eight invariants are checked after every step of every sweep and at the end of
every scenario: well-formed streams, well-formed provider requests, at most one
effect per tool call, agreement between the terminal event and the journal,
deadlines on every wait, stable replay, a session that stays usable, and no
process left running.

From the repository root:

```sh
python conformance/blackbox/run.py python typescript --quick    # the CI subset
python conformance/blackbox/run.py python typescript            # everything
python conformance/blackbox/run.py --list                       # case names
python conformance/blackbox/run.py python --only 'crash[rpc,k=7,resume]' --seed 1
```

The full run is 515 cases per language. A failure prints what was expected,
what happened and a command that reproduces that one case. After the
per-language counts the harness lists every case where two languages ended
differently. A language that is disabled or missing prints `NOT RUN` and is
never counted as passing. Run one harness at a time on a machine: heavy
parallel load causes timing failures.

### Adding a language

Enable or add one entry in [`blackbox/targets.py`](blackbox/targets.py): the
command that starts the language's CLI. The harness appends `--mode rpc`,
`serve` or `-p ...` itself and configures the process only through the
documented `TINYAGENT_*` environment variables and CLI flags. Entries for Go,
Rust, Zig, C#, Java, PHP and Elixir exist with `enabled=False`; each is switched
on when that language's port to protocol version 2 lands.

Beyond the wire protocol the harness relies on one thing: a stored session is
the file `<sessionId>.jsonl` in `TINYAGENT_SESSION_DIR`, one record per line.

### What it cannot see

The CLI's only tool is a non-idempotent bash and it exposes no hooks, so some
of the contract is invisible from outside. Each language's own suite must
cover:

- the `Storage` interface, `MemoryStorage`, custom stores, and a fenced append;
- rerun of an idempotent tool after a crash;
- `onUncertain: "reconcile"`;
- `beforeTool` and `afterTool`;
- pause and resume with a value;
- typed output, run `context`, and the provider options `headers` and `extra`;
- trace events and `ScriptedProvider`;
- tool timeouts, including the bash timeout, and the bash `env` option;
- a tool that returns an image;
- import and construction without I/O.

[The harness README](blackbox/README.md#covered-by-each-languages-own-suite)
maps each of these to the reason it is not black-box.

## What is verified

| Languages | State |
| --- | --- |
| Python, TypeScript | `run.py python typescript` and the harness in quick mode run in CI on Linux for pushes to `main` and for pull requests. Each language's own suite also runs in CI on Linux and macOS. The full harness run passes for both, with no divergences between them. |
| Go, Rust, Zig, C#, Java, PHP, Elixir | Protocol version 1. Not in CI and not run by the harness. Their checks were last run in September 2026 on macOS arm64 and have not been re-run since. Treat a pass as unknown until you run it. |
