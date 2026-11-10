# TinyAgent conformance fixtures

This directory holds the language-neutral fixtures that every implementation's
tests read, and `run.py`, which runs each language's native checks.

| File | What it holds |
| --- | --- |
| `rpc_v1.json` | RPC protocol version, byte limits, and RPC scenarios |
| `tool_arguments.json` | JSON Schema cases for tool argument validation |
| `providers/*.sse` | Synthetic SSE transcripts for OpenAI Responses and Anthropic Messages |
| `run.py` | The runner |

[PROTOCOL.md](../PROTOCOL.md) is the contract these fixtures test. When the two
disagree, fix them together.

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

The file covers the baseline commands only: `prompt`, `abort`, `new_session`
and `get_state`. It has no scenarios for durable sessions, recovery or
compaction. Python and TypeScript test those in their own suites
(`test_sessions.py` and `sessions.test.ts`).

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

## What is verified

| Languages | State |
| --- | --- |
| Python, TypeScript | `run.py python typescript` runs in CI on Linux for pushes to `main` and for pull requests. Each language's own suite also runs in CI on Linux and macOS. |
| Go, Rust, Zig, C#, Java, PHP, Elixir | Not in CI. Their checks were last run in September 2026 on macOS arm64 and have not been re-run since. Treat a pass as unknown until you run it. |
