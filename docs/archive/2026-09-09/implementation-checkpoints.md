# TinyAgent implementation history

Status: archived on 2026-09-09. These checkpoints record earlier states;
use the [finish contract](../../../FINISH_CONTRACT.md) for current acceptance.

## 2026-09-07 implementation checkpoint

TypeScript now exports `serve(agent, options)` and supports the `serve` CLI
command. Its HTTP adapter shares `Agent.run`, implements all five routes, and
keeps HTTP code in `tinyagents/typescript/http.ts`. Shared byte limits live in
`protocol.ts`. Socket behavior is not yet accepted.

The core now closes providers when a caller stops consuming events or its event
callback throws. Providers stop reading at their terminal event. Runs reject
cancellation even when a custom provider returns normally after abort. Prior
history is copied before a provider receives it, and oversized tool exception
messages become bounded observations. Six focused regressions verify these
cases in addition to the existing 19 TypeScript checks.

Python has the same lifecycle fixes and six corresponding regressions. From
the TinyAgent root, `python3 -m unittest discover -s tinyagents/python -p
'test_*.py'` passes all 26 tests. Its public package continues to use the
canonical `tiny_agent` core. Direct Python stream consumers must close an
iterator when leaving early; `Agent.run` handles this itself.

Run these commands from `tinyagents/typescript`:

| Check | Result |
| --- | --- |
| `npm run check` | Source and all tests pass strict type checking. Test checking was added in this checkpoint. |
| `node --test tinyagent.test.ts lifecycle.test.ts` | 25 passed. Includes RPC subprocess tests, provider fixtures, Bash cancellation, and lifecycle regressions. |
| `node tiny_agent.ts --self-test` | Passed. |
| `node --test http.test.ts` | All eight tests blocked at local TCP binding with `listen EPERM`; endpoint behavior remains unverified. |
| `./node_modules/.bin/tsc` | JavaScript and declarations emitted. Local `.ts` imports rewrite to `.js`. |
| `npm pack --ignore-scripts` plus an isolated local install | Packed SDK imported without provider credentials and completed a fixture tool loop. |
| Scoped Biome formatting | Passed using two-space indentation and 120-column lines. |
| Root `npm run check` | Blocked by pre-existing nested Biome root configurations in three unrelated scratch worktrees. |

No live provider request, registry publication, commit, or push occurred.

This checkpoint is superseded by the verification below where counts or scope differ.

## HTTP and argument validation checkpoint

Python now exports `serve`, implements all five routes, and supports the `serve`
CLI. Its HTTP transport lives in `tinyagent_http.py`; shared values live in
`tinyagent_types.py`. It uses bounded request parsing and synchronous event
writes, watches prompt disconnects, and waits for cooperative cleanup on deletion
and shutdown. All eight socket tests were attempted and fail at TCP binding with
`PermissionError`. Socket behavior remains unverified.

Both SDKs validate tool schemas at construction and arguments before handlers
run. Python pins `jsonschema==4.26.0` and `referencing==0.37.0`; TypeScript pins
Ajv 8.20.0. Their tests consume the same Draft 2020-12 argument fixture. Neither
validator fetches external schemas or modifies argument values.

Current verification:

| Check | Result |
| --- | --- |
| Python `unittest test_bash test_tinyagent test_lifecycle test_http_parsing test_http.ExposureTests test_tools` | 40 passed, using the locally installed Python 3.14 environment with jsonschema 4.26.0. |
| TypeScript `npm run check` | Source and all tests pass strict type checking. |
| TypeScript `node --test tinyagent.test.ts lifecycle.test.ts tools.test.ts` | 30 passed. |
| TypeScript compiler emit, tarball pack, isolated offline install | Passed; installed package validates arguments and completes a tool loop. |
| Python wheel build and isolated target install | Passed; installed wheel imports all runtime modules and completes a tool loop with the declared validator dependencies available. A fresh online dependency install was unavailable. |
| `conformance/run.py go rust zig java ruby elixir csharp php swift` | Exits 1. Reports Zig missing and the other eight SDK suites unfinished. |

`conformance/run.py` now runs native SDK checks with provider credentials removed.
It keeps the socket tests mandatory and reports missing runtimes and unfinished
SDKs as incomplete. It does not yet prove the eleven shared scenarios in every
language.

Remaining work includes broader SSE transport fixtures, HTTP socket acceptance,
eight other native runtimes, and the final quality and packaging audit.
Keep the contract open until every row is proved.

## Go core and provider checkpoint

This checkpoint predates the Go adapters below. Its adapter and CLI gaps are now
implemented; its real TCP verification gap remains.

Go now exposes an importable `tinyagent` module with typed events and history,
native tool handlers, run-scoped contexts, and OpenAI and Anthropic providers.
One core owns the model and tool loop. Provider serialization, SSE reading, and
tool validation live in separate files. The old standalone CLI remains runnable
until it is connected to this core; removing its duplicated loop is still open.

The SDK pins `github.com/kaptinlin/jsonschema` 0.9.2 and requires Go 1.26.4.
Standard Draft 2020-12 definitions are bundled with their license so schema
construction does not fetch URLs. Existing local module checksums were available
for an offline build; `go mod verify` reports all modules verified. No checksum
verification setting was disabled.

Focused Go checks pass with the race detector, excluding the separately attempted
`TestProvidersLocalHTTP`. They cover shared tool argument cases, sequential
recoverable tools, bounded observations, cancellation after provider return,
consumer failure, history isolation, both SSE parsers, opaque replay metadata,
terminal-event cleanup, and malformed or incomplete responses. The provider
fixtures use injected HTTP clients and synthetic SSE bytes. They do not prove
real TCP behavior.

`conformance/run.py go`, using the installed Go 1.26.4 runtime, executes the full
race suite and `go vet`. Vet passes. Both local HTTP provider tests fail at
`bind: operation not permitted`. The runner reports failure and also identifies
the missing Go CLI integration, RPC, and HTTP session server.

The Go example compiles through the module test command, and the original CLI
blacklist self-test still passes. Go files pass `gofmt`; the new Python modules,
HTTP and tool tests, and the conformance runner pass Black. The Python offline
suite passes all 40 tests after formatting and the Bash fixes below. The repository's exact
`npm run check` was attempted again and exits on the same three unrelated
nested Biome root configurations before checking source.

No commits or pushes were made. Temporary package installations and formatting
files created for these checks were removed from the package's scratch folder.

The final Python run exposed a background reader error when an output-limit stop
raced with an already-exited shell. Bash now confirms exit before ignoring a
denied signal and reports other stop failures to the caller. Cancellation also
signals the process group when the shell has exited but descendants still own
the pipes. Three focused regressions cover these cases, including a real
background `sleep` child. The final 40-test run had no thread exceptions.

## Go CLI, RPC, and HTTP checkpoint

Go's coding CLI now imports the native SDK. The standalone `tiny_agent.go` and
installed `cmd/tinyagent` command delegate to `cli.Run`; the old duplicated loop
is gone. A root `go.work` preserves the documented standalone command and its
working directory. Both providers can be selected with `TINYAGENT_PROVIDER`.

`BashTool` validates commands, bounds combined output, and cancels Unix process
groups even after the initial shell exits. RPC and HTTP use one private session
implementation for busy checks, history commit, cancellation, and cleanup.
RPC writes the prompt acknowledgement before starting events and uses bounded
UTF-8 JSONL framing. HTTP implements all five routes with bearer authentication,
explicit remote opt-in, bounded bodies and events, and synchronous writes.
Cancellation gives blocked HTTP writes a one-second grace period.

Checks used the installed Go 1.26.4 toolchain with `GOPROXY=off`. Commands below
run in `tinyagents/go` unless another directory is stated.

| Check | Result |
| --- | --- |
| `go test -race -timeout 90s -skip 'TestProvidersLocalHTTP\|TestHTTPServerTCP\|TestHTTPBackpressureTCP' ./...` | Passed. Includes core/provider tests, shared schema cases, native Bash, RPC subprocesses, ten HTTP handler test groups, and CLI option/history checks. |
| `go test -race -timeout 90s ./...` | Fails at four denied TCP binds: OpenAI and Anthropic provider fixtures, the HTTP route suite, and HTTP backpressure. Other checks pass. Real socket behavior is not accepted. |
| `go vet ./...` | Passed. |
| `go run ./cmd/tinyagent --self-test` | Passed. |
| `python3 conformance/run.py go` from the TinyAgent root | Exits 1 for the TCP failures. Also runs vet and CLI self-test; it no longer reports the implemented Go adapters as missing. |
| Copied-module import/build smoke with `GOWORK=off` | Passed. A separate consumer imports copied SDK source, rejects invalid arguments, runs a native tool loop, builds the CLI, and checks its self-test and RPC state command without credentials. Temporary copies were removed. |
| `gofmt -l tinyagents/go` from the TinyAgent root | No output. |
| `black --check conformance/run.py` from the TinyAgent root | Passed. |
| Root `npm run check` | Stops before source checks at the same three unrelated nested Biome root configurations. |

RPC subprocess checks cover the shared command fixture, ordered events and IDs,
escaped newlines, busy rejection, reset, failed-history rollback, oversized
output, invalid framing, EOF, and interrupt during a real Bash child. HTTP
handler checks cover authentication, validation, independent sessions, busy
rejection, rollback, cancellation, deletion waiting for provider cleanup,
disconnect contexts, output failures and limits, shutdown, and blocked writes.
Handler checks do not establish TCP behavior.

The finish contract remains active. Python and TypeScript still need the shared
SSE byte fixtures and their remaining protocol audit. All three implemented
servers need real socket acceptance. Rust's adapters remain open;
Zig, Java, Ruby, Elixir, C#, PHP, and Swift still need their native SDKs. No paid
requests, commits, or pushes occurred.

## Rust imported-core checkpoint

This checkpoint predates the streaming provider implementation below.

Rust now exposes an async `Agent`, native `Provider` streams, validated `Tool`
handlers, typed history and events, and run-scoped cancellation. The library
lives in `src/`; the original blocking CLI remains separate until streaming
providers can replace its model loop. CLI integration is therefore still open.

Eight tests pass, including the shared argument fixture, recoverable sequential
tools, bounded observations, provider failures, malformed or missing turns,
provider-stream cleanup, callback failure, and paired tool events on cancellation.
An imported SDK example completes a tool loop without credentials or network.

Verified with Rust 1.96.1 and `CARGO_NET_OFFLINE=true`:

| Check | Result |
| --- | --- |
| `cargo test --locked` | Eight tests pass. |
| `cargo clippy --locked --all-targets -- -D warnings` | Passed, including the offline example. |
| `cargo run --quiet --locked --example offline` | Prints `hello` after a native tool loop. |
| `cargo run --quiet --locked -- --self-test` | Original CLI blacklist check passes. |
| `cargo fmt --check` | Passed. |
| `python3 conformance/run.py rust` from the TinyAgent root | Exits 1 as incomplete after the above checks pass. Streaming providers and adapters remain open. |

Direct dependencies are pinned. `jsonschema` 0.30.0 validates Draft 2020-12 and
uses an explicit retriever that rejects external schemas. `futures-util` 0.3.32
and its matching macro crate were available locally; 0.3.34's macro was missing
and the attempted registry download could not resolve the host. The final
native build completed offline with normal Cargo checksum verification.

Next work is Rust streaming provider serialization and SSE parsing against the
shared fixtures, then native Bash, CLI integration, RPC, and HTTP. The remaining
language and socket requirements are unchanged. No contract requirement was
dropped to make these focused checks pass.

## 2026-09-08 Rust provider checkpoint

The CLI, Bash, and RPC gaps recorded here are implemented in the next checkpoint.

Rust now exports `OpenAIProvider` and `AnthropicProvider`. Both use native
async HTTP, buffer complete tool arguments, and preserve opaque replay metadata.
Their constructors require no credentials; requests resolve the configured key
or provider environment variable. The default request timeout is 180 seconds.

The providers share a bounded UTF-8 SSE reader and a small parser interface.
API-specific serialization and response state live in separate files. Tests
split every byte of the shared SSE fixtures, including UTF-8 characters and
delimiters. They exercise native tool loops, request headers and JSON bodies,
reasoning metadata, thinking signatures, usage merging, malformed tool JSON,
invalid encoding, byte limits, incomplete responses, and read errors.

Verified with Rust 1.96.1, `CARGO_NET_OFFLINE=true`, and `CARGO_INCREMENTAL=0`:

| Check | Result |
| --- | --- |
| `cargo test --locked -- --skip local_http` | Seventeen tests pass: nine provider checks and eight core checks. |
| `cargo clippy --locked --all-targets -- -D warnings` | Passed. |
| `cargo fmt --check` | Passed. |
| `cargo run --quiet --locked --example offline` | Prints `hello` after the fixture tool loop. |
| Original CLI `--self-test` | Passed. |
| `python3 conformance/run.py rust` | Exits 1. Both real TCP tests fail at denied local binding; the runner also reports native Bash and adapter work as incomplete. |
| Root `npm run check` | Stops at the same three unrelated nested Biome root configurations. |

The terminal-event test keeps the byte stream open after the provider finishes.
It verifies that the parser returns without waiting for EOF. Separate real TCP
tests exercise completion and cancellation with an unfinished chunked HTTP body.
Those tests could not bind here, so network cleanup remains unverified.

A file write failed while disk space was low. The source file remained intact.
Cargo's package-scoped clean removed generated TinyAgent artifacts, after which
editing and verification resumed. Incremental compilation was disabled for the
remaining checks; no unrelated build cache or source was removed.

Next work is Rust's cancellable Bash tool, CLI integration, RPC, and HTTP. The
remaining seven native SDKs, broader Python/TypeScript transport audit, real
socket acceptance, and final quality/package audit are still required. The
finish contract remains active.

## 2026-09-08 Rust CLI and RPC checkpoint

The HTTP gap recorded here is implemented in the next checkpoint.

Rust's CLI now calls the imported async SDK for both interactive and RPC modes.
The duplicated blocking model loop is gone, and reqwest's unused blocking
feature was removed. OpenAI and Anthropic selection uses the same environment
variables as the other implemented CLIs. The entrypoint registers interrupt
and termination handling before starting work and exits without waiting for
another stdin read after cancellation.

The native Bash tool validates commands, bounds combined stdout/stderr, and
stops Unix process groups. Tests cover a background child retaining pipes
after its shell exits. The private session module owns busy checks, history
commit, cancellation, and cleanup for use by RPC and the upcoming HTTP adapter.

RPC acknowledges each prompt before starting events. Its bounded line reader
retains partial input when another branch of the driver completes. Oversized
events and replies emit a bounded protocol error and cancel the affected run
while leaving the connection usable. Output I/O failure stops the driver.
Cancellation gives a blocked writer one second to drain. Dropping the driver's
future cancels its active provider.

Verified with Rust 1.96.1, `CARGO_NET_OFFLINE=true`, and `CARGO_INCREMENTAL=0`:

| Check | Result |
| --- | --- |
| `cargo test --locked -- --skip local_http` | Thirty-three tests pass: eleven provider/CLI checks, three Bash checks, eight core checks, nine RPC subprocess groups, and two RPC cleanup checks. |
| `cargo test --locked --test rpc` | All nine groups pass after extending oversized replies to cover an active run. |
| `cargo clippy --locked --all-targets -- -D warnings` | Passed. |
| `cargo fmt --check` | Passed. |
| `cargo run --quiet --locked --example offline` | Prints `hello`. |
| `cargo run --quiet --locked -- --self-test` | Passed using the SDK-backed CLI. |
| `cargo package --offline --allow-dirty` | Creates and verifies the local crate. The offline example and CLI self-test also pass from the extracted package source. |
| `python3 conformance/run.py rust` | Exits 1: both real TCP tests fail at denied local binding. All other tests pass; the runner also identifies Rust HTTP as incomplete. |
| `black --check conformance/run.py` | Passed. |
| Root `npm run check` | Stops before source checks at the same three unrelated nested Biome root configurations. |

The subprocess tests consume the shared RPC command fixture and verify ordered
events, IDs, escaped newlines, busy/abort/reset, failed-history rollback, framing
errors, output limits, partial commands, EOF, and interrupt with a real Bash
descendant. The actual CLI also answers state commands without credentials and
exits on interrupt while stdin remains open. Interactive checks verify native
tool replay, streamed output, and rollback after provider failure.

Rust HTTP and the final package audit remain open. Shared conformance fixtures
are still read from the source checkout. Cargo package verification passes with
a warning about absent license and publication metadata; that metadata has not
been invented or treated as approved. The remaining seven native SDKs, broader
Python/TypeScript transport audit, real socket acceptance, and final quality
audit are still required. No paid provider requests, commits, or pushes occurred.
The finish contract remains active.

## 2026-09-08 Rust HTTP checkpoint

Rust now exports `serve`, `HttpOptions`, and `HttpServer`, and the CLI
supports `serve`. All five routes use the same native core and private session
module as RPC. HTTP code lives in `src/http/`: routes and ownership in
`mod.rs`, body streaming in `body.rs`, and listener/connection lifetime in
`server.rs`. Hyper provides HTTP/1 framing; no second agent loop was added.

The server accepts one request per connection. A one-event channel applies
backpressure, and the terminal callback waits for the connection to finish
writing before a successful run can commit history. A failed terminal write
retains the previous history. Run cancellation also closes a stalled connection
after a one-second grace period. Deletion removes the session from the map
before awaiting cleanup, so disconnecting the deletion request cannot retain
a closed session.

Options default to `127.0.0.1:8080`. Typed socket addresses, token validation,
constant-time bearer comparison, explicit remote opt-in, absent CORS, bounded
request bodies, and bounded JSONL events are implemented. `close().await`
stops accepting connections, cancels runs, and waits for cooperative cleanup.
Dropping the handle requests shutdown without waiting.

Verified with Rust 1.96.1, `CARGO_NET_OFFLINE=true`, and `CARGO_INCREMENTAL=0`:

| Check | Result |
| --- | --- |
| `cargo test --locked -- --skip local_http` | Forty-two tests pass. Includes eight HTTP groups and the new CLI option test alongside the prior core, provider, Bash, and RPC checks. |
| `cargo test --locked --lib http::` | All eight groups pass after simplifying request JSON decoding. |
| `cargo clippy --locked --all-targets -- -D warnings` | Passed. |
| `cargo fmt --check` | Passed. |
| `python3 conformance/run.py rust` | Exits 1. Forty-two tests pass and five fail at denied TCP binding: two provider tests, two SDK server tests, and one CLI server test. The runner no longer lists Rust HTTP as unimplemented. |
| `cargo package --offline --allow-dirty` | Local crate verification passes. Cargo still warns about absent license/publication metadata. |
| Packaged-source offline example and CLI self-test | Passed. |
| `black --check conformance/run.py` | Passed. |
| Root `npm run check` | Stops before source checks at the same three unrelated nested Biome root configurations. |

HTTP tests run native Hyper client/server connections over bounded in-memory
streams. They check authentication and duplicate credentials, routes, request
encoding and content type, fixed/chunked body limits, ordered events, busy
sessions, independent histories, provider failures, oversized output,
terminal-write failure, disconnect rollback, cancellation during Bash, deletion
waiting for tool cleanup, disconnected deletion, bounded production for a
stalled reader, connection release, and shutdown. These checks exercise HTTP
framing and lifecycle but do not prove operating-system TCP behavior.

The separate TCP suites remain mandatory. Packaging's source-checkout fixture
dependency and metadata still need final review. Seven native SDKs, the broader
Python/TypeScript transport audit, real socket acceptance, and the final
requirement audit remain open. Java's existing complete-response CLI was
inspected, and OpenJDK 25.0.1 is available for the next native implementation.
No paid provider requests, commits, or pushes occurred. The contract stays active.

## 2026-09-08 Java provider checkpoint

Java now has native OpenAI Responses and Anthropic Messages providers under
`src/main/java/dev/tinyagent/`. The package separates shared HTTP lifetime,
bounded SSE framing, and provider-specific serialization and parsing. Public
records preserve immutable neutral history, completed calls, provider output,
and opaque replay metadata. Providers run synchronously on the caller's thread.
Their constructors require no credentials; requests resolve the provider key.

Cancellation closes pending HTTP requests and body reads. A request deadline
stays armed through body parsing and callback delivery. Interrupted header
waits initially left their HTTP future pending; a regression exposed this and
the implementation now explicitly cancels it before propagating interruption.
Cancellation also continues through all registered callbacks when two callbacks
throw the same exception instance.

The module targets Java 21 and pins Jackson databind/core 3.0.2 and annotations
2.20. Executable tools and schema validation are not implemented. A suitable
native Draft 2020-12 validator was not present in the local dependency cache,
and Maven Central DNS resolution failed. This remains an implementation and
dependency gap; provider-facing tool metadata is not a validated tool.

Verified with OpenJDK 25.0.1, compiling with `--release 21 -Xlint:all -Werror`:

| Check | Result |
| --- | --- |
| `python3 tinyagents/java/check.py --offline` | Twelve provider groups pass. The helper compiles runtime classes, creates a JAR, and compiles/runs checks against the packaged JAR. |
| Full native provider checks | Twelve groups pass; both real TCP groups fail at `SocketException: Operation not permitted` during binding. |
| `python3 conformance/run.py java` | Exits 1 for those TCP failures and explicitly reports the missing Java core and adapters. The original CLI self-test also passes in this run. |
| `java TinyAgent.java --self-test` from `tinyagents/java` | Original CLI blacklist check passes. |
| Cached Maven 3.9.11 `--offline --batch-mode --no-transfer-progress -DskipTests package` | Stops before compilation because the resources plugin's transitive dependencies are absent. A fresh Maven build is not verified. |
| `black --check conformance/run.py tinyagents/java/check.py` | Passed. The packaged JAR contains runtime classes only. |
| Root `npm run check` | Stops before source checks at the same three unrelated nested Biome root configurations. |

The offline provider checks use the JDK's real InputStream body subscriber and
one-byte fixture delivery. They cover both shared text/tool transcripts,
request headers and bodies, reasoning metadata, signatures and merged usage,
history snapshots, UTF-8/JSON errors, multiline and CRLF framing, byte limits,
malformed/incomplete turns, cancellation before headers and during bodies,
timeouts, thread interruption, consumer failure, terminal return without EOF,
and caller-owned HTTP client lifetime. They do not prove network behavior.

The shared runner now executes these Java checks and the original CLI self-test.
It continues to report Java as incomplete for its native agent core, validated
executable tools, CLI integration, RPC, and HTTP. Maven's test lifecycle does not
run the provider checks; `check.py` is the documented test entrypoint.
The original Java CLI remains unchanged until it can use the native core.

The full eleven-language contract remains active. No paid provider calls,
commits, or pushes occurred.

## 2026-09-08 Python provider and package audit checkpoint

Java's schema dependency is still unavailable: the validator is absent from
local caches and a fresh Maven Central check failed DNS resolution. Work moved
to Python's required shared-provider audit; Java's native core and validated
tools remain open.

Python now consumes all four shared SSE transcripts through its native HTTP
response parser, with byte fragmentation and actual tool loops. Provider code
moved out of `tiny_agent.py` into `tinyagent_openai.py` and
`tinyagent_anthropic.py`. Shared HTTP lifetime, strict JSON/UTF-8 framing,
and byte bounds live in `tinyagent_sse.py`. The public imports still resolve
to the same `Agent` core; both provider modules are included in the wheel.

The audit corrected unbounded SSE lines/events and argument accumulation,
duplicate or malformed calls, OpenAI argument lookup for sparse output indices,
and Anthropic acceptance of incomplete stop reasons and invalid block order.
Anthropic replay now retains opaque fields from the original message as well
as thinking signatures and merged usage. Two old synthetic event fixtures were
completed with their missing start/stop metadata.

Provider constructors resolve credentials at request time and accept endpoint
and timeout options. The native HTTP transport shuts down the active socket
before closing the response reader, including when a `Connection: close`
response has detached the socket from its connection. A deadline remains armed
during response parsing; connection establishment uses the standard library's
socket timeout. Consumer failure and terminal completion close unfinished
response bodies.

Verification uses the installed Python 3.14 environment with the declared
jsonschema/referencing versions. Wheel building uses the bundled Python's
setuptools backend without downloading dependencies.

| Check | Result |
| --- | --- |
| Focused Python core, Bash, RPC, parsing, exposure, schema, lifecycle, and provider checks | Fifty-one tests pass, including eleven new provider groups. |
| Full `conformance/run.py python` | Fifty-one tests pass; ten fail at denied TCP binding: eight server tests and two provider tests. The CLI self-test passes. |
| Wheel build, isolated target install, import and provider smoke | All seven runtime modules load from the installed wheel; the eleven provider groups pass against it, using existing validator dependencies. |
| Black checks | Passed for all Python sources and tests. |
| Root `npm run check` | Stops before source checks at the same three unrelated nested Biome root configurations. |

The full runner keeps all TCP tests mandatory and exits nonzero here. Native
HTTP framing tests do not establish TCP acceptance. The first attempted wheel
builder, local `uv`, panicked while reading macOS system configuration;
the direct setuptools build and installed-wheel checks passed.

TypeScript's shared SSE adoption was still open at this checkpoint; the next
checkpoint covers that work. The remaining native SDKs, real TCP acceptance,
and the final requirement audit remain required. No paid provider
calls, commits, or pushes occurred. The finish contract remains active.

## 2026-09-08 TypeScript provider and package audit checkpoint

TypeScript now consumes all four shared SSE transcripts through native
`Request`, `Response`, and `ReadableStream` objects, including one-byte
fragmentation and native tool loops. Provider implementations moved from
`tiny_agent.ts` into `openai.ts` and `anthropic.ts`. Shared values and tool
validation live in `types.ts`; bounded SSE framing and HTTP lifetime live in
`sse.ts`. Existing public imports still use the same Agent core and barrel.

The audit corrected unbounded line/event and tool-argument accumulation,
malformed and duplicate calls, sparse OpenAI output-index handling, and
Anthropic acceptance of incomplete stop reasons or invalid block order.
Anthropic replay retains opaque message fields, thinking signatures, and merged
usage. Existing synthetic Anthropic tests now supply their missing start/stop
metadata.

Provider constructors resolve keys at request time and accept endpoint,
timeout, and fetch options. The deadline remains armed while streaming and
delivering callbacks. A regression caught buffered completion succeeding after
a slow callback outlived the deadline; cancellation is now checked between
buffered lines and events. Cleanup cancels the response body once, awaits that
same cancellation promise, and releases the reader lock on every exit.

Verified using Node.js 26.7.0 and TypeScript 5.9.3:

| Check | Result |
| --- | --- |
| SDK `npm run check` | Source and all test files pass strict type checking. |
| Offline core, lifecycle, schema, RPC, Bash, and provider tests | Forty-five tests pass, including fifteen provider groups. |
| Full `conformance/run.py typescript` | Forty-five tests pass; ten fail at denied TCP binding: eight server tests and two provider tests. CLI self-test passes. |
| Direct compiler emit and `npm pack --ignore-scripts` | Tarball contains all seven runtime modules with declarations; no tests or fixtures. |
| Isolated offline install and strict consumer check | Passed using cached dependencies. The consumer runs both providers through native tool loops, validates arguments, checks replay metadata and body cleanup, and imports the public API without credentials. |
| Installed CLI self-test | Passed. |
| Scoped Biome format check | Passed for all ten changed TypeScript files using two-space indentation and 120-column lines. |
| Root `npm run check` | Stops before source checks at the same three unrelated nested Biome root configurations. |

The fixture transport verifies Fetch stream ownership, not TCP behavior. All
socket tests remain mandatory; the full runner exits nonzero here. Fresh
online dependency installation was not verified. Seven native SDKs, remaining
adapter audits, real socket acceptance, and the final requirement audit remain
open. No paid provider requests, commits, or pushes occurred. The finish
contract remains active.

## 2026-09-08 TypeScript RPC output audit checkpoint

RPC code now lives in `rpc.ts`, with the existing public exports preserved.
The core and coding CLI remain in `tiny_agent.ts`; this extraction reduced
that entrypoint to 452 lines. The emitted package now contains eight runtime
modules and their declarations.

Two regressions exposed real failures: a broken output stream left the driver
waiting for open input, and an oversized command response prematurely ended
the connection. Output errors and closure now cancel the active provider and
stop input. Oversized command responses produce a bounded protocol error,
cancel the run, and leave the connection available for subsequent commands.
Pending writes reject on output closure; already closed outputs fail before
input is read. Driver cleanup preserves listeners owned by the caller.

| Check | Result |
| --- | --- |
| Full `conformance/run.py typescript` | Both strict type checks pass. Forty-nine tests pass; the same ten TCP tests fail at `listen EPERM`. CLI self-test passes. |
| Four focused RPC regressions | Pass for output failure, oversized-response recovery, output closure during a write, and an already closed output. The first two failed before the fixes. |
| Direct compiler emit, tarball pack, isolated offline install | Passed. All eight runtime modules and public imports load without credentials. No tests or fixtures ship in the tarball. |
| Installed package checks | Strict consumer type checking, all four RPC regressions, and CLI self-test pass. |
| Scoped Biome format check | Passed for the entrypoint, RPC module, and new RPC tests. |
| Root `npm run check` | Stops at the same three unrelated nested Biome root configurations before source checks. |

The next checkpoint covers the remaining RPC cancellation and interrupt work
recorded here. HTTP TCP acceptance, seven remaining native SDKs, and the final
requirement audit remain open. No paid provider calls, commits, or pushes
occurred. The contract remains active.

## 2026-09-08 TypeScript RPC cancellation checkpoint

RPC cancellation now gives outstanding writes one second to drain before
closing the transport. This releases providers held behind a blocked consumer
after abort, reset, EOF, fatal input, or an oversized reply. A write that drains
within that period preserves the connection. The same rule covers an idle
command response when input has ended. Oversized output cancels the active run
before waiting to write its bounded protocol error.

`RpcSession` passes the run signal to custom emitters and tracks prompt
acknowledgement cleanup as part of the active run. Closing during a pending
acknowledgement waits for it and does not call the provider afterward.

Real subprocess tests exposed Node's special stdout behavior: destroying that
stream leaves its descriptor open, so a blocked pipe can keep the process alive
even after provider and driver cleanup. The standalone process adapter now
awaits `runRpc` before exiting. The CLI and test fixture share that adapter;
the imported stream driver retains no authority to exit its host process.

| Check | Result |
| --- | --- |
| Full `conformance/run.py typescript` | Both strict type checks pass. Sixty-four tests pass; the same ten mandatory TCP tests fail at `listen EPERM`. CLI self-test passes. |
| RPC output/session tests | Twelve groups pass, including sustained backpressure, drain recovery, and acknowledgement cleanup. The abort/backpressure regression failed before the fix. |
| RPC subprocess tests | Seven groups pass: EOF and Ctrl-C during provider work and Bash, coding CLI idle interruption, and both shutdown paths with a blocked stdout pipe. The pipe tests failed before the process adapter fix. |
| Bash process evidence | Both shell and background child PIDs exist before cancellation and disappear after shutdown. Started tools have matching aborted end events. |
| Direct compiler emit, pack, and isolated offline install | Eight runtime modules and declarations load. Strict consumer checks, all nineteen RPC groups, and installed CLI self-test pass. Tests and fixtures are excluded from the package. |
| Scoped Biome format check | Passed for all five changed TypeScript files. |
| Root `npm run check` | Stops at the same three unrelated nested Biome root configurations before source checks. |

The next checkpoint covers TypeScript's native HTTP lifecycle audit. Socket
acceptance, Python's adapter audit, the remaining seven native SDKs, and the
final requirement audit remain open. No paid provider requests, commits, or
pushes occurred. The contract stays active.

## 2026-09-08 TypeScript HTTP lifecycle checkpoint

HTTP server construction is separate from listener binding inside `http.ts`.
The public `serve` interface is unchanged and uses that same native server.
Tests connect Node's actual HTTP client and server through bounded byte streams;
the fixture's write callbacks acknowledge peer delivery and propagate write
failures. The TCP suites remain separate and mandatory.

The audit corrected history being committed before response framing finished,
cancellation waiting indefinitely on stalled writes, acceptance of duplicate
Authorization headers, and a stale cancel request succeeding after session
deletion. The terminal event now ends the HTTP response, and the run remains
busy until that write completes. Final framing failure retains prior history.
Cancelled writes close after a one-second drain period; cooperative tool
cleanup can take longer while a healthy response remains open.

| Check | Result |
| --- | --- |
| Full `conformance/run.py typescript` | Source and all tests pass strict type checking. Seventy-seven tests pass; the same ten mandatory TCP tests fail at `listen EPERM`. CLI self-test passes. |
| Native HTTP framing and lifecycle checks | Thirteen groups pass. They cover routes, exposure, authentication, duplicate headers, JSON/UTF-8 and fixed/chunked limits, independent histories, ordered events, busy/cancel behavior, rollback, final framing failures, deletion, tool cleanup, disconnects, and stalled readers. |
| Regression evidence | Premature history commit, stalled cancellation, duplicate authorization, and stale cancellation each produced a failing test before their corresponding fixes. |
| Compiler emit and tarball | All eight runtime modules and declarations are included; tests and fixtures are excluded. |
| Isolated installed-package checks | Strict consumer checks, public imports and `serve` option validation, all thirteen HTTP groups, and CLI self-test pass. |
| Scoped Biome format check | Passed for the HTTP implementation and all three new fixture/test files. |
| Root `npm run check` | Stops at the same three unrelated nested Biome root configurations before source checks. |

The first offline consumer install selected an uncached `fast-uri` 3.1.7.
The verified consumer instead pins transitive dependencies to the SDK lockfile's
versions, including `fast-uri` 3.1.6. The SDK manifest and lockfile were unchanged;
a fresh online dependency install is not verified.

Real TCP acceptance, Python's adapter audit, seven remaining native SDKs, and
the final requirement audit remain open. No paid provider calls, commits, or
pushes occurred. The finish contract remains active.

## 2026-09-08 Python RPC lifecycle and framing checkpoint

`tinyagent_rpc.py` now owns the RPC session and JSONL adapter; `tiny_agent.py`
retains the core, Bash tool, and CLI. Public package imports remain unchanged,
and the wheel includes all eight runtime modules.

Close, reset, and idle waits now track completion of the prompt acknowledgement
as well as the worker. Previously, closing or resetting during an acknowledgement
raised `cannot join thread before it is started`. Failed acknowledgements release
the run without invoking its provider. An emitter can request cancellation of its
own run without waiting on itself.

The writer now retries short writes and rejects a write that makes no progress.
The input parser rejects `NaN`, `Infinity`, and `-Infinity` while allowing the next
valid command. Regressions for acknowledgement races, truncated short writes,
missing write progress, and invalid JSON numbers failed before the corresponding
fixes. Existing history ownership was verified by holding or failing terminal
delivery and cancelling during that delivery. Tool cleanup still completes before
reset acknowledges success.

| Check | Result |
| --- | --- |
| Full `conformance/run.py python` | Sixty-one tests pass; ten mandatory TCP tests fail at `bind` with `Operation not permitted`. CLI self-test passes. |
| Focused RPC checks | Twenty tests pass, including the ten new lifecycle and framing groups. |
| Isolated wheel build and install | All eight runtime modules are included and import from the installed wheel. Tests are excluded from the artifact. |
| Installed-package checks | All ten new RPC groups, eleven provider groups, and CLI self-test pass. |
| Native pipe probe | Reproduced an open failure against the installed wheel: EOF did not release a process streaming a 100,000-byte delta into unread stdout within 1.5 seconds. Provider cleanup was not reached. The owned probe process was killed and reaped. |
| Black | All seventeen Python files pass. |
| Root `npm run check` | Stops before source checks at the same three unrelated nested Biome roots. |

The synchronous driver still needs native subprocess coverage and cancellation
for blocked input/output, including EOF, interrupt, fatal input, oversized command
replies, and hard output failures. Python's HTTP audit, real TCP acceptance,
seven remaining native SDKs, and the final requirement audit remain open.
No paid provider calls, commits, or pushes occurred. The finish contract remains
active.

## 2026-09-08 Python native RPC pipe checkpoint

The installed-wheel EOF hang recorded above is fixed. `tinyagent_rpc_io.py`
owns nonblocking native pipe I/O and one bounded output queue. Command replies
queue without blocking input. Agent events wait for delivery: the start event
holds provider work behind the prompt acknowledgement, and terminal delivery
holds history commit. This also fixes pipelined commands hiding abort or EOF
behind a stalled reply. Both pipeline regressions failed before the queue moved
to output.

Cancelled native writes have a one-second drain period. A stalled write fails
the connection and releases queued writers; a write that drains permits reuse
after abort. Queue overflow reserves room for one `output_queue_full` error,
cancels the run, and ends the connection while staying within 4 MiB. Oversized
objects emit `output_too_large` and cancel the run before waiting on output.
Native descriptor writes leave no pending Python stdout buffer to stall process
exit.

Ctrl-C now records intent for normal I/O polling. The interrupt test exposed
worker startup returning before provider cleanup when a Python asynchronous
interrupt crossed `Thread.start`; a deterministic startup-interruption regression
now verifies cleanup before return. Default signal handlers and descriptor
blocking modes are restored, custom signal handlers remain untouched, and
embedding callers retain stream ownership. Programmatic emitters can receive a
run cancellation token; `RpcSession` also accepts a parent cancellation token.

| Check | Result |
| --- | --- |
| Full `conformance/run.py python` | Eighty-one tests pass. The same ten mandatory TCP tests fail at `bind` with `Operation not permitted`. CLI self-test passes. |
| RPC checks | Thirty lifecycle, framing, ownership, queue, and native subprocess groups pass. |
| Native process evidence | EOF, abort, reset, Ctrl-C, fatal input, oversized output, stalled acknowledgement/replies, pipelined commands, and broken stdout release the process. Healthy drain permits reuse. Bash and its child exist before cancellation and are gone afterward, with paired tool events. |
| Isolated wheel build and install | All nine runtime modules import from the installed wheel; tests and subprocess fixtures are excluded from the artifact. |
| Installed-package checks | The thirty RPC groups, eleven provider groups, and CLI self-test pass. Subprocess fixtures are copied away from the SDK source and use installed modules and the installed CLI. |
| Black | All twenty Python files pass. |
| Root `npm run check` | Stops before source checks at the same three unrelated nested Biome roots. |

Native signal and process-group verification is POSIX-specific. Custom streams
without native descriptors must provide synchronous methods that return for
cleanup to advance. Python's HTTP audit, real TCP acceptance, seven remaining
native SDKs, and the final requirement audit remain open. No paid provider calls,
commits, or pushes occurred. The finish contract remains active.

## 2026-09-08 Python HTTP framing and cleanup checkpoint

HTTP request handling now has separate listener and application ownership.
`tinyagent_http.py` owns sessions, streaming responses, and shutdown;
`tinyagent_http_body.py` owns bounded body parsing. Both adapters still call the
same native agent core. The wheel contains all ten runtime modules.

Prompt responses use chunked HTTP framing. The final chunk must finish writing
before history commits or the session becomes idle. A failed final write retains
the previous history. Cancelled writes have one second to drain before the
connection shuts down; the deadline exists only while a write is pending.
Deletion and server shutdown wait for cooperative tool cleanup, including when
deletion is already underway. A healthy response remains open during that cleanup.

Authentication rejects duplicate Authorization headers. Cancellation rechecks
session deletion after reading its body. Body parsing rejects duplicate or empty
Transfer-Encoding, conflicting framing headers, and oversized decimal lengths
without unbounded integer conversion. Duplicate authorization, stale cancellation,
missing final-chunk ownership, and ambiguous framing were reproduced before their
fixes. Body framing and lifecycle checks use the standard-library HTTP client and
handler over bounded in-memory streams; they do not establish TCP acceptance.

| Check | Result |
| --- | --- |
| Full `conformance/run.py python` | 103 tests: 93 pass; ten mandatory TCP tests fail at `bind` with `Operation not permitted`. CLI self-test passes. |
| Focused HTTP checks | Nineteen parser, route, lifecycle, and exposure groups pass. They cover one terminal event, rollback, busy state during final framing, stalled output, tool disconnects, and cleanup during deletion and shutdown. |
| Isolated wheel build and install | Exactly ten runtime modules are packaged and import from the installed target. Test modules and fixtures are excluded from the wheel. |
| Installed-package checks | Sixty tests pass: nineteen HTTP, thirty RPC, and eleven provider groups. The installed CLI self-test passes. Tests and their fixture data are copied away from the source tree. Declared validator dependencies come from the existing Python environment; a fresh online dependency install is not verified. |
| Black | All twenty-four Python files pass. |
| Root `npm run check` | Stops before source checks at the same three unrelated nested Biome roots. |

Real TCP acceptance, seven remaining native SDKs, and the final requirement audit
remain open. No paid provider calls, commits, or pushes occurred. The finish
contract remains active.

## 2026-09-08 Ruby provider checkpoint

Ruby now has native OpenAI Responses and Anthropic Messages providers. Six
runtime files separate immutable provider values and cancellation, bounded SSE
framing, HTTP lifetime, and each API's serialization and replay. The public gem
entrypoint imports without credentials. Requests resolve credentials lazily and
run on the caller's thread with synchronous text callbacks.

Returned tool calls contain complete JSON argument objects. Replay preserves
OpenAI reasoning items and Anthropic thinking signatures, message metadata, and
merged usage. Mutable JSON is copied and frozen recursively. `ToolDefinition`
is provider metadata only; executable tools and schema validation remain open.

Cancellation and deadlines close pending header and body reads. Deadline threads
are joined and callbacks removed before return. A regression initially showed
consumer failure waiting for additional HTTP chunk bytes until the deadline.
The transport now closes before that framing read, preserves the consumer error,
and also releases requests on interruption. Complete provider terminal events
return without waiting for HTTP EOF.

| Check | Result |
| --- | --- |
| `ruby check.rb --offline` | Fourteen provider groups and 146 assertions pass on Ruby 3.1.0 with its native Net::HTTP and JSON libraries. |
| `conformance/run.py ruby` | Fourteen groups pass; two mandatory TCP groups fail at `bind(2)` with `Operation not permitted`. The original CLI self-test passes. The runner explicitly reports the missing native core and adapters. |
| Isolated gem build and install | All six runtime Ruby files package and import from the installed gem. Tests and the original CLI are excluded. RubyGems reports the optional homepage field is unset. The package includes the repository's MIT license. |
| Installed-package checks | All fourteen groups and 146 assertions pass with tests and SSE fixture data copied away from the source tree. |
| Ruby syntax and warnings | All six packaged runtime files pass `ruby -wc`. Running malformed-number tests with verbose warnings also exposes Ruby's own expected overflow warning for the rejected `1e999` input. |
| Black | Updated `conformance/run.py` passes. |
| Root `npm run check` | Stops before source checks at the same three unrelated nested Biome roots. |

The fixtures exercise Ruby's real HTTP serializer and parsers over in-memory
streams. They do not establish TCP behavior. Default connection establishment
uses the native timeout capped at 30 seconds; callbacks must return or cooperate
with cancellation. No paid provider request, commit, or push occurred.

Java's native schema validator is still absent from its local Maven cache, and a
fresh Maven Central request still fails DNS resolution. Ruby's core, validated
tools, CLI integration, RPC, and HTTP are the next stages for this language.
The full eleven-language finish contract remains active.

## 2026-09-08 Elixir provider checkpoint

Elixir now has native OpenAI Responses and Anthropic Messages providers. Eight
runtime source files separate immutable public values, cancellation, JSON
validation, bounded SSE framing, HTTP lifetime, and provider serialization and
replay. The SDK declares Elixir 1.15 or later and pins Mint 1.9.3 and Jason 1.4.5,
with HPAX 1.0.4 in its lock. Verification used Elixir 1.20.4 and OTP 29; the lower
version boundary is not verified. Cached dependency sources match their local
Hex archives.

Mint serializes HTTP/1 requests and parses responses. Each call owns a fresh
connection, a child cancellation token, and a 180-second default deadline.
The receive loop checks cancellation during stalled headers and body reads.
Callbacks run synchronously in the caller's process and must return before
cleanup can proceed. Native connection setup and request writes use timeouts
capped at 30 seconds. Cancellation does not forcibly interrupt DNS or TLS setup.
The connection closes before return, and cleanup drains only that socket's
messages. Cancellation or expiry during cleanup prevents a successful turn.

Shared fixtures preserve OpenAI opaque reasoning items and Anthropic thinking
signatures, message metadata, and merged usage. Parser checks reject incomplete
turns, malformed data, duplicate calls, and oversized SSE or tool arguments.
A failing HTTP fixture exposed rejection of `100 Continue` before the final
response. Informational statuses now proceed to the final response; protocol
switching remains rejected.

The ExUnit fixture replaces only Mint's socket transport, so offline checks
exercise its real HTTP serializer and parser. They do not prove TCP or TLS.
Two additional required tests use native TCP listeners and the default Mint
connector. They cannot bind in the current runner.

| Check | Result |
| --- | --- |
| Native format and compilation | All runtime and check files pass `Code.format_string!` comparison; eight runtime source files compile with warnings treated as errors. |
| `elixir check.exs --offline` with cached dependency builds | Eighteen tests pass. Two TCP tests are explicitly excluded. Coverage includes both providers, replay, framing, cumulative bounds, cancellation, deadlines, callbacks, cleanup, and unrelated caller messages. |
| `python3 conformance/run.py elixir` with cached dependency builds | Eighteen tests pass; two required TCP tests fail at listener creation with `:eperm`. The runner also builds the Hex package and reports missing core and adapters. |
| `mix hex.build --output dist/tiny_agent_sdk-0.0.1.tar` | Native Hex package builds successfully. It contains eight runtime files, `mix.exs`, `mix.lock`, README, and MIT license; tests, helpers, and the original CLI are excluded. |
| Isolated package checks | All twelve packaged files match source. The extracted package compiles, and all eighteen offline tests pass against it. All nineteen runtime modules load from the extracted package's compiled directory. |
| README example | The original snippet parses. Replacing only its provider connection with the local HTTP fixture executes the packaged provider and prints the expected UTF-8 text. No paid request runs. |
| Mix dependency installation and original CLI | `HEX_OFFLINE=1 mix deps.get` and `elixir tiny_agent.exs --self-test` fail because Mix's filesystem lock requires a TCP listener. Normal Mix compilation and the original CLI self-test remain unverified. |
| Black | The shared Python conformance runner passes. |
| Root `npm run check` | Stops before source checks at the same three unrelated nested Biome roots. |

The original Elixir CLI remains unchanged. Tool definitions are provider
metadata; schema validation and native handlers are not implemented. The
agent core, validated tools, CLI integration, RPC, HTTP, native socket
acceptance, and final audit remain open. No package publication, paid provider
request, commit, or push occurred. The finish contract remains active.

## 2026-09-08 C# provider checkpoint

C# now has native async OpenAI Responses and Anthropic Messages providers.
Eleven runtime files separate immutable messages and provider values, bounded
SSE framing, HTTP lifetime, and each API's request serialization and replay.
The .NET 8 library has no external package dependencies. Its public interface
uses `Task<ProviderTurn>`, awaited text callbacks, and native cancellation tokens.

Returned JSON values are cloned `JsonElement` snapshots. Shared fixtures verify
complete tool argument objects, preserved OpenAI reasoning items, Anthropic
thinking signatures and merged usage, and replay on the next request.
`ToolDefinition` is metadata only; schema validation and execution remain open.

Requests use a lazy API key and a default 180-second deadline. Cancellation,
deadlines, and consumer failure release header and body reads. Terminal events
return without waiting for HTTP EOF. An asynchronous-disposal regression first
reproduced a successful return after cancellation arrived during cleanup.
The transport now awaits disposal and checks cancellation before returning the
completed turn. The same boundary preserves deadline failure during cleanup.

Default clients disable cookies and redirects and close unread responses without
draining them. This also avoids the sandbox's failing native CookieContainer
initialization. Injected clients remain caller-owned. Callbacks must return or
honor their token; provider disposal requests cancellation, while the caller
still awaits active requests.

| Check | Result |
| --- | --- |
| Native release build | SDK, checks, and original CLI build with .NET SDK 8.0.303 and runtime 8.0.7, with zero compiler warnings or errors. Restore emits a macOS `CSSM_ModuleLoad` diagnostic but succeeds using installed SDK references and an empty package feed. |
| `python3 conformance/run.py csharp` | Fifteen provider groups pass. Two mandatory TCP groups fail at `Socket.Bind` with `Permission denied`. The original CLI self-test passes. The runner explicitly reports the missing core and adapters. |
| NuGet packaging | `TinyAgent.Sdk.0.0.1.nupkg` contains the compiled library, README, MIT license, and NuGet metadata. Tests, fixtures, and the original CLI are excluded. |
| Isolated installed-package checks | All fifteen provider groups pass in a separate consumer restored from the local NuGet package into its own package cache. Tests and SSE fixtures are copied away from the SDK source. The loaded library's SHA-256 matches the packaged library. |
| Documented example | The README's C# example compiles against that installed package without warnings. It is not executed against a paid API. |
| Formatters | Native .NET whitespace verification passes for runtime and check sources. Black passes for the C# build helper and shared conformance runner. |
| Root `npm run check` | Stops before source checks at the same three unrelated nested Biome roots. |

The offline fixtures use native .NET HTTP serialization and parsing over memory
streams. They do not establish TCP behavior. The original C# CLI source remains
unchanged and now also has a .NET 8 project entrypoint. Native core, validated
tools, CLI integration, RPC, HTTP, and real socket acceptance remain open.
No paid provider request, registry publication, commit, or push occurred.
The full eleven-language finish contract remains active.

## 2026-09-08 PHP provider checkpoint

PHP now has synchronous OpenAI Responses and Anthropic Messages providers with
complete tool calls, neutral history, and opaque replay data. Eleven runtime
files separate public values and cancellation, bounded SSE framing, the HTTP
client interface, native cURL I/O, request lifetime, and the two provider parsers.
The package requires PHP 8.4 and its JSON and cURL extensions. Tool definitions
remain metadata; schema validation and execution are not implemented.

JSON accessors return fresh object copies while retaining empty objects and
arrays as different values. A regression reproduced PHP reference aliases
changing an already-constructed history. Message, tool, and header snapshots
now detach those aliases. Shared fixtures preserve OpenAI reasoning metadata
and Anthropic thinking signatures, message metadata, and merged usage.

Each request owns a child cancellation token and a 180-second default deadline.
The child checks both its parent and its deadline. Parent cancellation wins over
ordinary failures, and a callback exception after deadline expiry still reports
the timeout. Tests cover cancellation and deadlines during byte delivery,
callbacks, and client cleanup. Completed turns return only after client cleanup;
the parser stops at the terminal event even if malformed bytes follow it.

The native client uses cURL's multi API to check cancellation while waiting. It
creates and releases handles per request, disables redirects, and aborts reading
when the provider reaches its terminal event. Native transport acceptance is
still open because neither TCP nor Unix socket listeners can bind in this runner.
The offline client fixture delivers bytes directly and does not prove cURL,
HTTP framing, or native connection cleanup.

| Check | Result |
| --- | --- |
| PHP syntax and CLI | Runtime files, local helpers, tests, and the original CLI pass syntax checks on PHP 8.4.3. The original CLI self-test passes. No PHP formatter is installed. |
| `php tests/check.php --offline` | Twelve provider groups pass, including both shared text/tool fixture pairs, malformed input, byte limits, detached snapshots, cancellation, deadlines, and cleanup. |
| `python3 conformance/run.py php` | Twelve groups pass; two required native cURL/TCP groups fail at listener creation with `Operation not permitted`. The runner reports the native core and adapters as incomplete. |
| Local ZIP packaging | Eleven runtime PHP files, `composer.json`, README, and the repository MIT license package successfully. Tests, helpers, and the original CLI are excluded. |
| Isolated package checks | All twelve groups pass with copied tests and fixtures outside the SDK source. All eleven runtime files load from the extracted package without credentials. Packaged files match source hashes. |
| README example | The unchanged example passes PHP syntax. With only its client replaced by the local byte fixture, it executes the packaged provider and prints the expected UTF-8 text. No paid request runs. |
| Composer | Manifest declares the native extension requirements and bootstrap file. Composer installation is unverified: Composer is absent, and Packagist and NuGet requests still fail DNS resolution. |
| Black | The shared Python conformance runner passes. |
| Root `npm run check` | Stops before source checks at the same three unrelated nested Biome roots. |

The original PHP CLI remains unchanged. Native core, validated tools, CLI
integration, RPC, HTTP, actual cURL/TCP acceptance, and the final audit remain
open. No registry publication, paid provider request, commit, or push occurred.
The full eleven-language finish contract remains active.

## 2026-09-08 Swift provider checkpoint

Swift now has native async OpenAI Responses and Anthropic Messages providers
for Apple platforms. Six runtime files separate typed JSON, immutable public
values, bounded SSE and argument parsing, URLSession lifetime, and the two
provider serializers and parsers. `Provider` uses a native async text callback;
all public request and result values are Sendable. The library uses no untyped
JSON containers. Tool definitions remain metadata, without schema validation or
native executable handlers.

Each request owns a fresh URLSession and a structured deadline. The provider
awaits callbacks and session invalidation, checks cancellation before returning,
and retains provider-specific replay fields. Callback cleanup can stay pending
after cancellation; the provider waits for it. The default deadline is 180
seconds. Cookies and caching are disabled, and the delegate rejects redirects.
The default session configuration is ephemeral; a custom configuration closure
must return a fresh configuration per request.

The native checks found and fixed three issues. Rejecting HTTP response headers
without advancing the returned suspended AsyncBytes reader prevented session
invalidation from finishing. The error path now cancels and settles that reader.
A deadline during request setup could start a byte request on an already
invalidated session; cancellation is checked after configuration, and AsyncBytes
owns cancellation while waiting for headers. Swift treats CRLF as one grapheme,
so character-based checks missed both characters. API keys now reject CR, LF,
and NUL by byte. Regressions cover each path.

A separate direct URLSession experiment, with no TinyAgent code, proved that
Foundation can deliver task completion and session invalidation before a custom
URLProtocol's stopLoading callback finishes. The SDK awaits session invalidation;
it does not promise to join work launched by a custom protocol. Offline fixtures
observe their own cleanup separately. Held text callbacks verify the SDK's
structured cancellation and awaited callback boundary. The fixture injects
parsed HTTP responses, so it does not prove HTTP wire parsing, TCP, or TLS.

| Check | Result |
| --- | --- |
| Native format and compiler | `swift-format lint --strict` passes for runtime sources, checks, and package manifest. Library and checks compile with Swift 6.2.1, Swift 6 language mode, warnings as errors, and an arm64 macOS 13 deployment target. Checks execute on the current macOS 26 host. |
| `python3 tinyagents/swift/check.py --offline` / equivalent isolated check | Seventeen provider groups pass. They cover both shared fixture pairs, request serialization, replay, typed JSON values, malformed and oversized arguments, SSE limits, cancellation, deadlines, HTTP failures, and callback cleanup. |
| `python3 conformance/run.py swift` | Seventeen groups pass; two mandatory native TCP groups fail at bind with `Operation not permitted`. The original CLI self-test passes. The runner reports core and adapters as incomplete. |
| iOS compiler check | The six runtime sources pass strict type checking for `arm64-apple-ios16.0-simulator`. No iOS runtime test has run. |
| Source packaging | The local archive contains six Swift runtime files, Package.swift, README, MIT license, and formatter configuration. It excludes tests, build helpers, and the original CLI. All ten packaged files match source. |
| Isolated package checks | The extracted source package compiles with the native compiler and passes all seventeen offline groups. The consumer binary links the library from that isolated package. |
| README example | The unchanged example passes strict Swift type checking. Replacing only its provider configuration with the local fixture compiles and prints the expected UTF-8 answer against the packaged library. |
| SwiftPM | `swift build` fails when its sandboxed manifest process starts: `sandbox-exec: sandbox_apply: Operation not permitted`. Normal package resolution and installation remain unverified. No sandbox-disabling flag was used. |
| Black | Both Python check helpers pass. |
| Root `npm run check` | Stops before source checks at the same three unrelated nested Biome roots. |

The original Swift CLI remains unchanged. Its separate loop and legacy untyped
JSON must be replaced during core integration before the finish contract can
close. Native core, validated tools, CLI integration, RPC, HTTP, real socket
acceptance, and the final audit remain open. No package publication, paid model
request, commit, or push occurred. The eleven-language finish contract remains
active.

## 2026-09-08 Ruby text-run core checkpoint

Ruby now exposes `Agent.run` and immutable `RunResult` values. The run accepts
prior history, a prompt, a run ID, and an optional parent cancellation token.
Its synchronous block receives ordered, frozen protocol event hashes. The
agent configuration is immutable; each invocation owns its history, sequence,
and child token. Different invocations can run concurrently when their provider
supports concurrent calls. The package now contains seven runtime files.

This is the core's text-run path. Executable tools cannot yet be registered.
An unexpected provider tool call fails with `provider_error` before any tool
execution event. The full tool loop, Draft 2020-12 validation, bounded tool
observations, Bash integration, RPC, and HTTP remain required. No weaker schema
validator or validation bypass was added. RubyGems still fails DNS resolution,
and JSONSchemer is absent from both inspected Ruby gem installations. Its
[version 2.5.0 documentation](https://github.com/davishmcclurg/json_schemer/blob/v2.5.0/README.md)
describes Draft 2020-12, schema validation, and configurable reference and format
handling, but no dependency adoption or validator execution is claimed.

The core snapshots inputs before `agent_start`, preserves provider replay data,
and returns new history only after provider cleanup and terminal callback
delivery. It never changes the supplied history. A callback exception or parent
cancellation during terminal delivery prevents a result from returning. There
is no session store yet; callers own history assignment and conversation access.
The tests do not establish server-level atomic commits or session busy handling.

Native callback-exit tests reproduced an existing Net::HTTP problem. Ruby 3.1's
chunk reader reads the trailing CRLF from an ensure block; closing the stream
during a callback's `break` replaced that control transfer with an I/O error.
The direct provider regression failed on chunked framing and passed with a fixed
content length. HTTP parsing now runs in one synchronous fiber and hands events
back to the caller's original fiber. Cleanup closes and finishes the private
reader without putting the caller's control flow inside native chunk cleanup.
No HTTP worker thread or output queue was added. The deadline thread remains
owned and joined by the request. Regressions verify `break`, `return`, `throw`,
exceptions, caller thread/fiber identity, and incomplete chunk cleanup.

| Check | Result |
| --- | --- |
| Offline provider and core checks | Thirty-two groups pass: fifteen provider groups and seventeen core groups. The latest isolated run has 480 assertions and no failures, errors, or skips. It covers both native provider fixtures, history reuse, snapshot isolation, event order, malformed values, provider errors, unknown tools, cancellation, concurrent runs, cleanup failures, and callback exits. |
| `python3 conformance/run.py ruby` | Thirty-two groups pass; two mandatory TCP groups fail at `bind(2)` with `Operation not permitted`. The original CLI self-test passes. The runner explicitly reports executable tools and their loop, CLI integration, RPC, and HTTP as incomplete. |
| Native Ruby syntax | Changed runtime and check sources pass `ruby -cw` on Ruby 3.1.0. No Ruby formatter is installed. |
| Gem packaging | The native gem builds and installs into an isolated gem home. All nine package files match source: seven runtime files, README, and MIT license. RubyGems emits the existing missing-homepage metadata warning. Tests and the original CLI are excluded. |
| Isolated package checks | All thirty-two groups pass with copied tests and shared fixtures. The runner verifies that the SDK loads from the isolated installed gem, outside the source package. |
| README examples | Both unchanged snippets pass Ruby syntax without warnings. Replacing only provider construction with the local fixture executes both the Agent and direct-provider examples against the installed gem and verifies HTTP cleanup. No paid API request runs. |
| Black | The updated shared conformance runner passes. |
| Root `npm run check` | Stops before source checks at the same three unrelated nested Biome roots. |

The original Ruby CLI remains unchanged. Tool execution, CLI integration,
RPC, HTTP, real TCP acceptance, and the final audit remain open. No registry
publication, paid provider request, commit, or push occurred. The full
eleven-language finish contract remains active.

## 2026-09-08 Ruby RPC checkpoint

Ruby now exposes `RpcSession` and `TinyAgent.run_rpc` for a configured native
Agent. The adapter implements all four RPC commands, IDs, error responses,
response-before-event ordering, busy rejection, reset, and completed session
history. A private session module owns admission, history, and its run worker.
The session stays active through acknowledgement and terminal delivery; failed
delivery or cancellation keeps the previously committed history. Reset waits
for cooperative cleanup before replying. Reentrant close requests cancellation
without trying to join its own callback.

Four modules keep these responsibilities separate: `session.rb` owns session
lifetime, `rpc.rb` maps protocol commands, `rpc_output.rb` owns bounded output,
and `rpc_io.rb` drives native input and output. The adapter calls `Agent.run`;
it contains no separate provider or tool loop. Executable tools and their loop
remain open, so these checks exercise configured text-run agents. The original
coding CLI has not yet adopted the SDK or this RPC adapter.

The driver reads commands on one thread while the caller writes output.
Acknowledgements and events await complete line delivery. Other replies enter
a 4 MiB queue so stalled stdout cannot hide abort or EOF. Input and output
objects have the shared 1 MiB limit. Invalid UTF-8 or oversized input closes the
connection. Oversized output produces `output_too_large` and cancels the run.
Queue overflow preserves an in-flight line, then emits `output_queue_full` and
stops. Cancelled output gets one second to drain. A connection that drains
after an abort can start another run; stalled output fails and releases waiters.

The driver joins its input and run workers, retains caller ownership of native
IO streams, and restores their blocking modes. It catches Ruby's default
Interrupt exception without replacing signal handlers. A direct native probe
explained an initially overbroad test assertion: Darwin adds the kernel's
`FWASWRITTEN` bit after a descriptor is written, even without TinyAgent, and
`F_SETFL` does not erase that marker. The corrected test starts with blocking
pipes and verifies restoration of `O_NONBLOCK` and stream ownership. The marker
is defined in [Apple's XNU source](https://github.com/apple/darwin-xnu/blob/main/bsd/sys/fcntl.h).
No platform-specific compatibility branch was added to the runtime.

| Check | Result |
| --- | --- |
| Offline native checks | Fifty-two groups pass with 579 assertions: fifteen provider, seventeen core, seven session, and thirteen pipe groups. There are no failures, errors, or skips. |
| Session lifecycle | Shared idle/invalid command fixtures pass. Tests verify acknowledgement order, history reuse and rollback, busy/abort handling, held acknowledgement ownership, failed terminal delivery, reset cleanup, and reentrant close. |
| Native subprocesses and pipes | Real Ruby processes verify JSONL framing, completed history, busy/abort/reset behavior, EOF and Ctrl-C, strict JSON and UTF-8, byte limits, failed output, stalled acknowledgements and replies, and reuse after cancelled output drains. A bounded native-pipe check verifies queue overflow while a line is already partially written. |
| `python3 conformance/run.py ruby` | Fifty-two groups pass; two required provider TCP groups fail at `bind(2)` with `Operation not permitted`. The unchanged coding CLI self-test passes. The runner reports executable tools and their loop, CLI integration, and HTTP as incomplete. |
| Ruby syntax and Black | All new runtime and test files pass `ruby -cw` on Ruby 3.1.0. The shared Python runner passes Black. No Ruby formatter is installed. |
| Gem packaging | Native gem build and isolated installation pass. All thirteen files match source: eleven runtime files, README, and MIT license. The existing missing-homepage metadata warning remains. |
| Isolated package checks | All fifty-two groups pass with copied tests and fixtures. An import assertion runs in the test process and its Ruby subprocesses to verify that they load the isolated installed SDK. |
| README examples | All three snippets pass Ruby syntax. Agent and direct-provider examples run with only provider construction replaced by the local fixture. The RPC snippet runs unchanged against the installed gem, handles `get_state`, and exits on EOF without credentials or a provider request. |
| Root `npm run check` | Stops before source checks at the same three unrelated nested Biome roots. |

These checks establish native RPC behavior for the implemented text-run core.
They do not establish executable tool events or Bash cancellation over RPC,
coding-CLI integration, HTTP behavior, or provider TCP acceptance. Those items
and the final audit remain open. No registry publication, paid model request,
commit, or push occurred. The full eleven-language finish contract remains active.

## 2026-09-08 Java core and session checkpoint

Java now exposes `Agent`, typed `AgentEvent` values, and `RunResult`. Immutable
configuration uses the existing native provider interface. Each run owns its
history, child cancellation token, ID, and event sequence. Completed history
and JSON output are isolated from caller mutation. Provider and callback
failures prevent a returned result; usable sinks receive an ordered terminal
event. Cleanup precedes terminal delivery, and cancellation during that delivery
also prevents returning a result. Unregistered tool calls fail before execution.

The private `Session` module owns one virtual-thread worker and committed
history. Admission remains busy through final delivery and cleanup. Failed or
cancelled runs retain prior history; reset clears history and waits for old work.
Shutdown stops admission, cancels outside the session lock, and joins the worker.
Callbacks can request shutdown without joining themselves. Waiting preserves
the caller's interruption flag. No RPC or HTTP adapter is exposed yet.

| Check | Result |
| --- | --- |
| `python3 tinyagents/java/check.py --offline` | Thirty-six groups pass: twelve provider, fourteen core, and ten session groups. Runtime and checks compile with Java 21 compatibility, `-Xlint:all`, and `-Werror` on OpenJDK 25.0.1. |
| `python3 conformance/run.py java` | Thirty-six groups pass; both mandatory provider TCP groups fail during binding with `SocketException: Operation not permitted`. The original CLI self-test passes. The runner explicitly reports missing executable tools, their loop, CLI integration, RPC, and HTTP. |
| Isolated JAR | All thirty-six groups pass with copied tests, shared fixtures, and the three exact declared Jackson JARs. The package contains forty-one class files from eighteen runtime sources, README, and MIT license. Packaged classes and metadata match the source build byte for byte; no test classes or CLI source are included. |
| README and import checks | The unchanged Java snippet compiles against the isolated JAR. With provider construction replaced by the native fixture in its test package, it streams `done\ncafé` and returns two committed messages. Both default provider agents construct and close without credentials or requests. |
| Python formatting | `black --check tinyagents/java/check.py conformance/run.py` passes. No Java formatter is available locally; compiler warnings remain errors. |
| Root `npm run check` | Stops before source checks at the same three unrelated nested Biome roots. |

The direct JDK packaging helper now includes README and license and removes
provider credentials from native checks. Maven declares the same package
resources. The earlier Maven plugin-cache blocker remains unresolved; this
checkpoint does not claim a successful Maven build. Temporary package-check
files were removed after verification.

Executable tools and their Draft 2020-12 validation, the tool loop, coding-CLI
integration, RPC, HTTP, real TCP acceptance, and final audit remain open. The
original Java CLI is unchanged. No registry publication, paid model request,
commit, or push occurred. The eleven-language finish contract remains active.

## 2026-09-08 Java RPC checkpoint

Java now exposes `Rpc.runStdio`, `Rpc.run`, and `RpcSession`. Command mapping
uses the existing native session and Agent; it implements prompt, abort, reset,
state, command IDs, errors, and response-before-event ordering. Successful
terminal delivery precedes history commit. Failed delivery and cancellation
retain prior history, and reset waits for cooperative cleanup.

`RpcOutput` owns bounded serialized lines. Acknowledgements and events await
complete delivery; other replies queue so blocked stdout cannot prevent input
from reaching abort or EOF. Input/output objects are limited to 1 MiB and queued
frames to 4 MiB. Oversized output cancels the run. Queue overflow preserves the
current frame, discards pending frames, and ends with a protocol error when the
peer can receive it. Cancelled output gets one second to drain; successful
draining permits another run on the same connection.

The channel adapter explicitly takes ownership of blocking interruptible
channels and closes them before returning. It joins its reader, writer, session
worker, and deadline tasks. `runStdio` adds a JVM shutdown hook for Ctrl-C
cleanup. The caller continues to own the provider. A native subprocess probe on
OpenJDK 25.0.1 found that a virtual-thread stdin read did not release on
interruption; platform threads released both blocked reads and blocked writes.
The driver therefore uses platform threads for I/O without a version branch.
Channel ownership follows the [JDK FileChannel interruption contract](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/nio/channels/FileChannel.html),
which closes the channel when interrupted.

| Check | Result |
| --- | --- |
| Native offline checks | Forty-nine groups pass: twelve provider, fourteen core, ten session, and thirteen RPC groups. Java 21 compilation on OpenJDK 25.0.1 passes with all warnings treated as errors. |
| RPC subprocesses and native pipes | Shared idle/error fixtures, completed history, failure rollback, busy/abort/reset, strict JSON and UTF-8, exact input limits, partial final lines, output bounds, EOF, Ctrl-C, blocked acknowledgements, queue overflow, reuse after cancelled output drains, broken output, channel ownership, and caller interruption pass. |
| `python3 conformance/run.py java` | Forty-nine groups pass; both mandatory provider TCP groups fail at denied local binding. The original coding-CLI self-test passes. The runner reports executable tools and their loop, CLI integration, and HTTP as incomplete. |
| Isolated JAR | All forty-nine groups pass, including their subprocesses, with copied fixtures and the three exact Jackson dependencies. Fifty runtime class files from twenty-three source files, README, and license match the source build byte for byte. Test classes and the original CLI are excluded. |
| README examples | Both unchanged snippets compile against the isolated JAR. The Agent snippet runs with native fixture provider construction. The unchanged RPC snippet handles `get_state` and EOF without credentials, provider requests, or stderr output. |
| Formatting and repository check | Black passes for both Python runners. Whitespace/conflict-marker checks pass for the RPC files. Root `npm run check` stops before source checks at the same three unrelated nested Biome roots. |

Temporary probes and package-check files were removed after verification. Maven
packaging retains its earlier plugin-cache blocker and was not claimed as
verified here. Executable tools, Draft 2020-12 validation, coding-CLI integration,
HTTP, provider TCP acceptance, and the final audit remain open. No paid provider
request, registry publication, commit, or push occurred. The full finish
contract remains active.

## 2026-09-08 Java HTTP transport investigation

The installed JDK 25.0.1 HTTP server does not expose the connection lifecycle
needed for prompt disconnects and final-delivery failure. Inspection of its
`ServerImpl` source found that active exchanges are removed from read selection
until the response completes. `ChunkedOutputStream.close` catches and ignores
I/O errors while sending the final chunk. An adapter around these APIs would
leave those finish-contract requirements unproved.

The available Netty binaries in the Gradle cache are 4.1.110.Final. That version
predates subsequent HTTP parsing security fixes, including the
[malformed Transfer-Encoding advisory](https://github.com/netty/netty/security/advisories/GHSA-38f8-5428-x5cv).
No Netty dependency was added. A fresh request to Maven Central still failed
DNS resolution. Java HTTP remains open pending a suitable transport dependency;
work continued on the required C# core.

## 2026-09-08 C# core checkpoint

C# now exposes `Agent.RunAsync`, immutable typed `AgentEvent` variants, and
`RunResult`. Runs use the existing native provider interface. Each owns its
transcript, cancellation scope, ID, and event sequence. Completed history and
JSON output are detached snapshots. Failed and cancelled runs leave caller
history unchanged. Tool calls fail as unregistered tools; executable handlers
and their loop are not implemented.

Event callbacks are awaited. Ordinary callback failures retain their original
exception and stack even when a provider wraps or swallows the error. A failed
consumer receives no later events. Native response disposal and run cancellation
callbacks finish before terminal delivery. The core retains the first
`CancelAsync` task so it also awaits callbacks started by caller cancellation.
Cancellation during final event delivery prevents a completed result.

Text callbacks receive the provider token, preserving native deadlines. Start
callbacks receive the run token; terminal callbacks receive the caller token,
which may already be cancelled for an aborted event. The caller continues to
own the provider and must await active runs before disposing it.

| Check | Result |
| --- | --- |
| Native release build and offline checks | Twenty-nine groups pass: fifteen provider and fourteen core groups. SDK, checks, and original CLI build with .NET SDK 8.0.303/runtime 8.0.7 and zero compiler warnings or errors. The existing macOS `CSSM_ModuleLoad` restore diagnostic remains; restore succeeds with installed SDK references and an empty feed. |
| Core lifecycle | Both providers, ordered typed events, immutable history/replay/output, failures, unknown tools, pre-cancellation, native header/body cancellation, preserved consumer errors, terminal delivery failure, held callbacks, provider deadlines, cancellation callback cleanup, asynchronous response disposal, concurrent runs, and invalid inputs pass. |
| `python3 conformance/run.py csharp` | Twenty-nine groups pass; both mandatory TCP groups fail at `Socket.Bind` with permission denied. The original CLI self-test passes. The runner reports executable tools and their loop, CLI integration, RPC, and HTTP as incomplete. |
| NuGet and isolated consumer | The package contains the compiled library from fourteen runtime source files, README, license, and NuGet metadata, with no external package dependencies or test/CLI source. All twenty-nine groups pass after installation into a separate package cache. A runtime path and SHA-256 assertion verifies the loaded library against the packaged DLL. |
| README examples | Both unchanged C# snippets compile against the installed package without warnings. Replacing only provider construction with a native fixture executes both examples; added harness assertions verify response cleanup. The Agent example returns two messages and streams `done\ncafé`. |
| Formatting | Native .NET whitespace verification passes for the changed runtime and test sources. Black passes for both Python runners. |
| Root `npm run check` | Stops before source checks at the same three unrelated nested Biome roots. |

Temporary package-check files were removed after verification. The original C#
CLI is unchanged. Executable tools, Draft 2020-12 validation, session/transport
adapters, coding-CLI integration, TCP acceptance, and the final audit remain open.
No paid provider request, registry publication, commit, or push occurred. The
full eleven-language finish contract remains active.

## 2026-09-08 C# session and RPC command checkpoint

C# now exposes `RpcSession.HandleAsync` for the four protocol commands. A private
`Session` owns committed history, admission, cancellation, and the worker task.
The command mapper uses the existing Agent and native providers. A prompt
acknowledgement finishes before Agent events begin; final delivery and cleanup
finish before history commits. Failure and cancellation preserve prior history.
Reset clears history, requests cancellation, and awaits the old run.

`Cancel` signals the active run. `Stop` also closes admission; `DisposeAsync`
awaits cleanup. The first native `CancelAsync` task is retained so cleanup waits
for callbacks already started by cancellation. An async execution context
prevents a callback from awaiting itself during reset or disposal. The external
owner still awaits cleanup before disposing its provider. Concurrent sessions
have independent history and run cancellation.

Emitters receive detached protocol objects and a nullable cancellation token.
Run acknowledgements and events have a token and await delivery. Ordinary
command responses can enter a transport-owned bounded queue. Byte framing,
output bounds, EOF, signals, and native I/O remain the next RPC adapter work;
the programmatic mapper does not claim to implement them.

A subprocess probe on .NET runtime 8.0.7 tested redirected stdin/stdout with
the peer deliberately stalled. Console reads and writes remained pending after
cancellation; disposing the console stream returned without joining those
operations. A `FileStream` opened over either inherited handle with asynchronous
mode enabled rejected the handle. `AnonymousPipeClientStream` reads and writes
reported cancellation, but then blocked inside `Dispose`; both probe processes
had to be terminated after their three-second deadline. These APIs alone did
not prove the stdio cleanup contract. No driver that abandons pending native
I/O was added.

| Check | Result |
| --- | --- |
| Native build and checks | SDK, checks, and original CLI compile without warnings or errors. Forty-three groups pass: fifteen provider, fourteen core, and fourteen RPC session groups. The original CLI self-test passes. |
| Session behavior | Shared command fixtures, acknowledgement ordering/failure/cancellation, busy rejection, final-delivery commit, failure rollback, abort, held cleanup, reset, disposal, callback shutdown, independent sessions, native provider cancellation/deadlines, detached values, and invalid commands pass. |
| `python3 conformance/run.py csharp` | Forty-three groups pass; both mandatory provider TCP groups fail at denied `Socket.Bind`. The runner explicitly reports executable tools and their loop, CLI integration, RPC byte transport, and HTTP as unfinished. |
| Isolated NuGet installation | All forty-three groups pass using a separate feed and package cache. A runtime path and SHA-256 check verifies the loaded library against the packaged DLL. The library from sixteen runtime source files, README, and license match the source build; there are no external package dependencies or test/CLI sources in the package. |
| README examples | All three compile unchanged against the installed package. The Agent and provider examples execute with fixture provider construction and verified native response cleanup. The new RPC state example executes unchanged without credentials or a provider request. |
| Formatting | Native .NET whitespace verification passes for both new runtime files and changed test sources. Black passes for both Python runners. |
| Root `npm run check` | Stops before source checks at the same three unrelated nested Biome roots. |

The original C# CLI remains separate pending executable tools and integration.
RPC transport, HTTP, tools and Draft 2020-12 validation, TCP acceptance, and the
final audit remain open. No paid provider request, registry publication, commit,
or push occurred. The full eleven-language finish contract remains active.

## 2026-09-08 C# native RPC checkpoint

The stdio investigation now has a verified resolution: the earlier blocked
disposal involved borrowed pipe handles. A follow-up subprocess probe gave the
native stream ownership of its handle. Reads and writes then cancelled and
disposed promptly, for both anonymous and named pipe wrappers. The implementation
uses `AnonymousPipeClientStream` with ownership; it needs no interop shim or
additional package. Seekable redirected files use `FileStream`.

`Rpc.RunAsync` now runs JSONL over two owned streams whose asynchronous I/O
honors cancellation. `Rpc.RunStdioAsync` opens redirected stdin/stdout on Linux
and macOS, installs a temporary Ctrl-C handler, and returns 130 for Ctrl-C or
zero for EOF. Native subprocess verification ran on macOS with .NET SDK 8.0.303
and runtime 8.0.7. Windows and terminal-attached stdio are explicitly unsupported
by this adapter. Stream and provider callbacks must cooperate with cancellation.
The provider remains caller-owned.

The driver shares the existing command mapper and Agent. It waits for its input
task, session, output task, and cancellation callbacks before returning, then
disposes the streams. `RpcOutput` bounds complete serialized frames to 4 MiB,
including the current write, and enforces the 1 MiB object limit. Input counts
UTF-8 bytes before decoding, rejects malformed JSON, and processes a partial
final line before EOF shutdown.

Oversized output becomes a bounded protocol error and cancels the run while
allowing connection reuse. Queue overflow retains the current frame, discards
pending frames, and attempts a final protocol error. Cancelled writes have one
second to drain; a successful drain preserves the connection. Stalled or broken
output cancels input and provider work and releases owned pipes. Final frame
writing and flushing precede history commit.

The queue-overflow regression waits for observed provider cancellation before
draining stdout. This establishes that overflow happened while the peer was
stalled; merely finishing an input write does not establish that the child has
processed that command. The acknowledgement-drain check similarly observes the
first output byte before sending abort. No sleep is used to manufacture either
condition.

| Check | Result |
| --- | --- |
| Native build and offline checks | Fifty-six groups pass: fifteen provider, fourteen core, fourteen session, and thirteen RPC groups. SDK, checks, and original CLI compile without warnings or errors. The CLI self-test passes. |
| Native RPC | Shared commands, file redirection, ordered completion, history rollback, busy/abort/reset, strict JSON/UTF-8, exact input limits, partial final input, oversized event and command output, EOF, Ctrl-C, stalled acknowledgements, reuse after draining, queue overflow, broken output with stdin still open, flush failures, and caller cancellation pass. |
| `python3 conformance/run.py csharp` | Fifty-six groups pass; both mandatory TCP groups fail at denied local `Socket.Bind`. The runner reports executable tools and their loop, CLI integration, and HTTP as incomplete. |
| Isolated NuGet | All fifty-six groups pass against a separate feed and package cache, including child processes and redirected files. Runtime path and SHA-256 assertions verify the loaded library in the parent and every child. The DLL from twenty runtime source files, README, and license match the source build; no external dependencies or test/CLI sources are packaged. |
| README examples | All four compile unchanged against the installed package. Agent and provider examples execute with fixture provider construction and verified cleanup. Both RPC examples execute unchanged without credentials; the stdio example handles `get_state` and EOF with no stderr output. |
| Formatting and repository check | Native .NET whitespace verification and both Python runner Black checks pass. Root `npm run check` stops before source checks at the same three unrelated nested Biome roots. |

Temporary probes and isolated package-check files were removed after verification.
The original C# coding CLI is unchanged. Executable tools, Draft 2020-12
validation, CLI integration, HTTP, TCP acceptance, and the final audit remain
open. No paid provider request, registry publication, commit, or push occurred.
The full eleven-language finish contract remains active.

## 2026-09-08 Swift core checkpoint

Swift now exposes an immutable `Agent`, typed `AgentEvent` values, and completed
`RunResult` snapshots. Its existing OpenAI and Anthropic streaming providers
execute text runs through this core. Executable tools remain open; an unexpected
tool call fails as an unregistered tool without returning partial history.

Each call owns one structured child task using `async let`, a transcript, run
ID, and event sequence. Parent cancellation propagates into the run; cancellation
inside its callback stays within that run. Concurrent calls share configuration
but no mutable history or cancellation state. Swift value semantics preserve
completed history and nested provider output when callers modify their copies.

Event delivery awaits each callback. The core retains the original consumer
error even when a provider wraps or swallows it, and sends no later event to a
failed consumer. Provider cleanup finishes before terminal delivery. A failed
or cancelled terminal callback cannot return a completed result or emit a second
terminal event. Custom providers must await their callbacks in order and finish
cleanup before returning; callbacks must cooperate with cancellation.

The native check dispatcher now consistently uses `@Sendable` callbacks.
Mixing its old callback array type with the new core cases compiled but caused
a runtime function-cast failure; matching the types resolved it.

| Check | Result |
| --- | --- |
| Native compile and offline checks | All thirty-one groups pass: seventeen provider and fourteen core groups. Swift 6 compilation treats warnings as errors; strict native formatting passes. |
| Core lifecycle | Both providers, shared event objects, value semantics, replay, provider errors, unregistered tools, pre-cancellation, native request cancellation, callback errors, deadlines, held cleanup, final delivery, and concurrent runs pass. |
| `python3 conformance/run.py swift` | Thirty-one groups pass; both mandatory TCP groups fail at denied local binding. The original CLI self-test passes. The runner reports executable tools and their loop, CLI integration, RPC, and HTTP as incomplete. |
| Isolated source package | All thirty-one groups pass using separately copied checks and fixtures. All thirteen archive entries match source bytes: nine runtime Swift files plus the manifest, README, license, and formatter configuration. No test or CLI files are packaged. Runtime loader traces and SHA-256 checks verify the extracted package's compiled library in every executed consumer. |
| README examples and import | Both examples compile unchanged against the extracted package and execute with only provider construction replaced by a native fixture. Response cleanup and output pass. Import and both provider constructors require no credentials. |
| Apple targets | The packaged library compiles for macOS 13 and all nine runtime files pass an iOS 16 simulator type check. Normal SwiftPM acceptance remains open under the previously recorded manifest-sandbox restriction. |
| Repository checks | Both Python runners pass Black. Root `npm run check` stops before source checks at the same three unrelated nested Biome roots. |

Temporary package consumers and compiler probes were removed after verification.
The original Swift CLI remains separate. Validated executable tools, CLI
integration, RPC, HTTP, native TCP, SwiftPM acceptance, and the final audit remain
open. No paid provider request, registry publication, commit, or push occurred.
The full eleven-language finish contract remains active.

## 2026-09-08 Swift session checkpoint

Swift now has a private `Session` actor shared by transport adapters and a
public `RpcSession` command mapper. The actor owns committed history, admission,
the active task, reset, and shutdown. `RpcSession` is a reference type with
immutable configuration. It accepts parsed `JSONValue` commands for prompt,
abort, reset, and state; byte framing remains separate pending work.

An admitted task delivers its acknowledgement before calling `Agent.run`.
The session stays busy through final-event delivery and cleanup. Only successful,
uncancelled runs commit history. Reset clears history, cancels and awaits the
old run, and rejects overlapping work. Stop closes admission and signals the
run; close also waits for cleanup, including when its calling task is cancelled.
The provider remains caller-owned.

Native task-local storage identifies callbacks executing within the active run.
Reset or close from those callbacks requests cancellation without waiting on
itself; the external owner still awaits cleanup. The installed Swift compiler's
`@TaskLocal` macro process failed at the sandbox boundary. The implementation
uses the standard library's public `TaskLocal` initializer, `get`, and `withValue`
methods directly. No macro plugin or sandbox-disabling flag is needed.

Emitters receive value snapshots and a delivery marker. Run acknowledgements
and events must finish delivery before returning; command replies may enter a
bounded transport queue. The mapper validates command fields, finite JSON
numbers, and nesting depth. It does not claim native JSONL framing, byte limits,
output queues, EOF, signals, or stdio cleanup.

| Check | Result |
| --- | --- |
| Native compile and offline checks | Forty-five groups pass: seventeen provider, fourteen core, and fourteen session groups. Strict Swift 6 compilation and formatting pass. |
| Session behavior | Shared command fixtures, delivery classification, acknowledgement ordering/failure/cancellation, final-delivery commit, rollback, busy handling, abort, reset, shutdown, callback self-wait avoidance, independent sessions, snapshots, invalid JSON values, and native provider cancellation/deadlines pass. |
| Cancellation evidence | Checks record whether a provider was entered after pre-cancellation or failed acknowledgement. Callback shutdown checks verify completion after reset/close, rather than merely reaching the callback. |
| `python3 conformance/run.py swift` | Forty-five groups pass; both mandatory TCP groups fail at denied local binding. The original CLI self-test passes. Executable tools, CLI integration, RPC byte transport, and HTTP remain explicitly incomplete. |
| Isolated source package | All forty-five groups pass with copied checks and fixtures. Fifteen archive entries match source bytes: eleven runtime Swift files plus manifest, README, license, and formatter configuration. Runtime loader traces and SHA-256 checks verify the package library in each executed consumer. |
| README examples | All three compile unchanged against the extracted package. Agent and provider examples run with fixture provider construction and verified native response cleanup. The RPC state example runs unchanged without credentials or a provider request. |
| Apple targets and repository checks | The package compiles for macOS 13; all eleven runtime files pass an iOS 16 simulator type check. Both Python runners pass Black. Root `npm run check` stops at the same three unrelated nested Biome roots. SwiftPM acceptance remains open. |

Temporary package consumers were removed after verification. The original
Swift CLI is unchanged. Tools and Draft 2020-12 validation, CLI integration,
native RPC, HTTP, TCP acceptance, SwiftPM, and the final audit remain open.
No paid provider request, registry publication, commit, or push occurred.
The full eleven-language finish contract remains active.

## 2026-09-08 Swift native RPC checkpoint

Swift now exposes `Rpc.run(agent:input:output:)` over owned native handles and
`Rpc.runStdio(agent:)` for macOS stdin/stdout. Both use the existing `RpcSession`
and `Agent` core. The transport closes both handles after joining input,
session cleanup, output, Dispatch readiness sources, and retained write-deadline
tasks. Caller cancellation propagates after cleanup. The stdio adapter
temporarily handles SIGINT, restores the previous signal action, and returns
130 on Ctrl-C. The generic handle adapter leaves signal ownership with its caller.

Descriptors use nonblocking native reads and writes with Dispatch readiness
notifications. Output suppresses SIGPIPE per descriptor, so a broken pipe
becomes an I/O error and cancels provider work even while stdin remains open.
The connection takes exclusive handle ownership immediately, including on
validation failure. Callers must not use the handles or their aliases concurrently.

JSONL objects are limited to 1 MiB excluding LF. The output queue includes the
current complete frame and is limited to 4 MiB including LF. Command replies
can queue while input continues; prompt acknowledgements and events await
delivery. History commits only after successful final delivery. Oversized
output emits a bounded error and cancels the run. Queue overflow cancels the
run, retains the current write, drops pending frames, and attempts a final
error before closing. Cancelled writes have one second to drain. Readiness
sources and deadline tasks are awaited before descriptors can be closed or reused.

The new runtime code separates connection lifetime, descriptor I/O, bounded
output, macOS signal handling, and the public transport error. The session
retains responsibility for commands and committed history. There is no second
agent loop, external runtime dependency, polling timer, or global SIGPIPE handler.

| Check | Result |
| --- | --- |
| Native compile and offline checks | Fifty-eight groups pass: seventeen provider, fourteen core, fourteen session, and thirteen native RPC groups. Strict Swift 6 compilation and native formatting pass. |
| Native transport | Shared command fixtures, completion ordering, correlation, committed history, rollback, busy/reset/abort, malformed JSON recovery, partial final input, exact multibyte input limits, invalid UTF-8, oversized output, and connection reuse pass through real subprocess pipes. |
| Shutdown and output bounds | EOF, Ctrl-C, stalled acknowledgement, draining output, queue overflow, broken output with input open, caller cancellation, SIGINT restoration, owned regular files, and invalid-handle cleanup pass. The overflow check observes provider cancellation before draining stdout, then verifies only the current frame and final queue error remain. |
| `python3 conformance/run.py swift` | Fifty-eight groups pass; both mandatory TCP groups fail at denied local binding. The original CLI self-test passes. The runner reports tools and their loop, CLI integration, and HTTP as incomplete. |
| Isolated source package | All fifty-eight groups pass with copied checks and fixtures. Twenty archive entries match source bytes: sixteen runtime files plus manifest, README, license, and formatter configuration. The parent and every RPC child verify the extracted library's loaded path and SHA-256; each example's loader trace and hash also match. |
| README examples | All four compile unchanged against the extracted package. Agent and provider examples run with fixture provider construction and verified response cleanup. The session-state and native-stdio examples run unchanged without credentials or provider requests. |
| Apple targets | The extracted SDK compiles for macOS 13 and all sixteen runtime files pass iOS 16 simulator type checking. The stdio entrypoint is macOS-only. SwiftPM acceptance remains open under the previously recorded manifest-sandbox restriction. |
| Repository checks | Both Python runners pass Black. Root `npm run check` stops before source checks at the same three unrelated nested Biome roots. |

Temporary native-I/O probes and package consumers were removed after verification.
Tools and Draft 2020-12 validation, coding-CLI integration, HTTP, native TCP,
SwiftPM acceptance, and the final audit remain open. No paid provider request,
registry publication, commit, or push occurred. The full eleven-language finish
contract remains active.

## 2026-09-08 PHP core checkpoint

PHP now exposes `Agent::run` with immutable configuration, typed `AgentEvent`
values, and a `RunResult` containing completed history and detached provider
output. Each invocation owns its history, event counter, generated or supplied
run ID, and child cancellation token. The existing synchronous providers remain
responsible for request I/O, SSE, replay, and cleanup.

Event callbacks run synchronously in the caller's thread and fiber. The core
retains the original consumer exception even when a provider wraps or swallows
it and stops calling a failed consumer. Provider cleanup precedes the terminal
event. Completed-event delivery must finish before a result returns; failed or
cancelled delivery cannot return a result or cause a second terminal event.

Cancellation propagates from the caller to the run and provider. Callback
tokens can cancel the run without cancelling the caller's token. A focused
check exposed a provider swallowing cancellation of its separate callback token
and returning success. The core now also cancels its run token in that path.
Provider deadlines remain provider failures. Retained provider callbacks cannot
emit after provider completion, including while final delivery is pending.

The implementation adds three runtime files: the core, typed events, and
completed results. It reuses the existing message and JSON value boundaries.
The core starts no scheduler or background work; embedding applications own
fiber scheduling and must resume suspended fibers for cooperative cleanup.
Executable tools remain separate required work, and an unexpected tool call
currently fails as an unregistered tool.

| Check | Result |
| --- | --- |
| PHP syntax and offline checks | All runtime, check, and original CLI files pass native syntax checks. Twenty-six offline groups pass: twelve provider and fourteen core groups. |
| Core and values | Both providers, ordered deltas, replay on a later run, typed protocol events, supplied/generated IDs, immutable results, detached PHP references, safe UTF-8 failure events, invalid inputs, provider failures, and unregistered tools pass. |
| Lifecycle | Original and swallowed consumer failures, pre-cancellation, callback cancellation at start/delta/final delivery, ignored cancellation, deadlines during request/callback/cleanup, held final delivery, held provider cleanup, independent fiber runs, and retained callbacks pass. |
| `python3 conformance/run.py php` | Twenty-six groups pass; both mandatory native cURL/TCP groups fail at denied local binding. The original CLI blacklist self-test passes. The runner explicitly keeps executable tools, CLI integration, RPC, and HTTP incomplete. |
| Isolated ZIP | All twenty-six groups pass using copied checks and fixtures. Seventeen archive entries match source bytes: fourteen runtime PHP files including the autoloader, plus manifest, README, and license. Every installed runtime file is verified by loaded path and SHA-256, and reflection rejects a class loaded from outside that package. |
| README and import | Both original README examples pass syntax checks and execute against the isolated package with fixture-only provider construction. Output and response cleanup pass. Import and both provider/agent constructors require no credentials and start no CLI. |
| Repository checks | The conformance runner passes Black. Root `npm run check` stops before source checks at the same three unrelated nested Biome roots. No PHP formatter or static analyzer was available on PATH; native syntax and runtime checks are the verified PHP gates. |

Temporary package consumers were removed. Native cURL/TCP, Composer acceptance,
schema-validated tools and their loop, coding-CLI integration, RPC, HTTP, and
the final audit remain open. No paid provider request, registry publication,
commit, or push occurred. The full eleven-language finish contract remains active.

## 2026-09-08 Elixir core checkpoint

Elixir now exposes `Agent.new` and synchronous `Agent.run`, typed `AgentEvent`
values, and immutable `RunResult` values. Runs accept prior history, a supplied
or generated run ID, an event callback, and cancellation. The existing provider
struct and its `stream/3` function own request I/O and cleanup. The core adds
three source files without introducing an external dependency or a background
process. The application explicitly declares its use of OTP's `:crypto`.

Each run owns child cancellation tokens, history, and event order. Provider
callbacks execute in the caller's process. A unique, temporary process-dictionary
entry retains the event counter and first consumer failure because the provider
callback interface does not return updated state. The entry is removed on every
cooperative exit. Nested runs use distinct keys; concurrent callers have
independent histories and cancellation. Caller messages and existing process
state remain intact.

The core preserves consumer errors, throws, and exits even when a provider
wraps or suppresses them. A failed consumer receives no later event. Provider
cleanup precedes terminal delivery, and completed delivery must finish before
a result returns. Failure or cancellation during that delivery cannot produce
a result or a second terminal event. Cancellation of a callback's separate
request token also cancels the run, while leaving its caller's token unchanged.
Retained callbacks cannot emit after provider completion. Custom providers and
callbacks must cooperate with cancellation and return for cleanup to finish.

| Check | Result |
| --- | --- |
| Native format and compilation | Eleven runtime source files and all check files pass the native formatter. Runtime compilation treats warnings as errors; the final check run emits no compiler warnings. Verification uses Elixir 1.20.4; the declared 1.15 lower boundary remains unverified. |
| Offline core and providers | Thirty-eight tests pass: eighteen provider and twenty core tests. They cover both providers, HTTP/SSE fixtures, ordered protocol events, final text, replay, immutable results, invalid input, provider failures, and unregistered tools. |
| Core lifecycle | Original and swallowed consumer failures, errors/throws/exits, pre-cancellation, callback cancellation at start/delta/completion, ignored cancellation, native header/body waits, deadlines, held cleanup, held final delivery, concurrent calls, nested runs, and retained callbacks pass. Temporary callback-state keys are absent after completion and failure. |
| `python3 conformance/run.py elixir` with cached dependencies | Thirty-eight tests pass; two mandatory TCP tests fail at listener creation with `:eperm`. The original CLI self-test also fails because Mix's filesystem lock requires a listener. Tools and their loop, CLI integration, RPC, and HTTP remain explicitly incomplete. |
| Native Hex packaging | `mix hex.build` succeeds. All fifteen packaged files match source: eleven runtime source files, project manifest, dependency lock, README, and license. The CLI, tests, and helpers are excluded. |
| Isolated package | Copied checks and fixtures pass all thirty-eight offline tests against the extracted package. All twenty-three runtime modules are checked before and after execution against package source hashes, loaded BEAM paths, and loaded-code MD5 values. |
| README and import | All three original Elixir code blocks parse. Both runnable examples execute against the package with only provider construction replaced by the native HTTP fixture; output, model selection, and cleanup pass. Both provider/agent constructors load without credentials or CLI startup. |
| Repository checks | The conformance runner passes Black. Root `npm run check` stops before source checks at the same three unrelated nested Biome roots. |

Temporary formatter and package consumers were removed after verification.
Validated tools and their loop, coding-CLI integration, RPC, HTTP, normal Mix
installation/compilation, native TCP, and the final acceptance audit remain open.
No paid provider request, registry publication, commit, or push occurred.
The full eleven-language finish contract remains active.

## 2026-09-08 Elixir session checkpoint

Elixir now exposes `RpcSession` for parsed `prompt`, `abort`, `new_session`, and
`get_state` commands. It reuses a private `Session` controller and the existing
native agent core. Each linked controller owns immutable committed history,
one monitored run, admission, reset, and cancellation. Provider work and event
delivery run outside the controller, so commands remain responsive while an
acknowledgement, provider, or terminal callback is held.

Acknowledgement finishes before provider entry. Successful history commits only
after terminal delivery and worker exit; provider failure, cancellation, or
delivery failure retains prior history. Reset clears history immediately and
holds admission until cleanup finishes. Close cancels and joins both processes.
Reset or close from a run callback requests cleanup without waiting on itself;
an external close still joins it. Normal owner exit also cancels and waits for
cooperative cleanup. Providers and callbacks must return for cleanup to finish.

The mapper validates native JSON values and optional string IDs. A focused
check found an improper Elixir list reaching `Enum.all?`; array validation now
rejects an improper tail. Another check found that an emitter raising
`SessionError` was retried as a protocol error. Command execution now maps
session errors before delivery, so all emitter failures propagate without a
second attempt. Run acknowledgement and event failures likewise receive no
later callback.

The emitter receives a cancellation token for run acknowledgements and events,
which must await delivery, and `nil` for ordinary command replies. Native byte
transport must still serialize concurrent writes, bound queued output, frame
JSONL, and own disconnect, EOF, and interrupt cleanup. This checkpoint does not
claim those transport requirements are implemented.

| Check | Result |
| --- | --- |
| Native format and compilation | Thirteen runtime files and all test/check files pass native formatting and compilation with warnings treated as errors on Elixir 1.20.4. |
| Offline checks | Fifty-three tests pass: the previous thirty-eight core/provider tests and fifteen session tests. Shared parsed-command fixtures, response-before-event order, pending acknowledgement, busy/state/abort, history commit/rollback, failed emitters, invalid native values, reset, close, callback-initiated cleanup, owner exit, independent sessions, and native provider cancellation/deadlines pass. |
| `python3 conformance/run.py elixir` with cached dependencies | Fifty-three tests pass. Both mandatory TCP tests fail at listener creation with `:eperm`; the original CLI self-test fails at Mix's TCP filesystem lock. The runner explicitly keeps RPC byte transport, tools and their loop, CLI integration, and HTTP incomplete. |
| Native Hex package | `mix hex.build` succeeds. All seventeen packaged files match current source: thirteen runtime files, manifest, lock, README, and license. |
| Isolated package | Copied checks and shared fixtures pass all fifty-three offline tests against the extracted package. All twenty-six runtime modules match installed source hashes, loaded BEAM paths, and loaded-code MD5 values before and after the suite. |
| README and import | All four original Elixir blocks parse. All three runnable examples pass against the extracted package; provider examples replace only provider construction with the native HTTP fixture. Output, model selection, and response cleanup pass. The session example matches the shared idle-state response and leaves no controller alive. Both provider/agent constructors load without credentials or CLI output. |
| Repository checks | The conformance runner passes Black. Root `npm run check` stops before source checks at the same three unrelated nested Biome roots. |

Temporary formatter and package consumers were removed after verification.
Validated tools and their loop, coding-CLI integration, native RPC byte transport,
HTTP, normal Mix installation/compilation, TCP, and the final audit remain open.
No paid provider request, registry publication, commit, or push occurred.
The full eleven-language finish contract remains active.

## 2026-09-08 Elixir native I/O checkpoint

The internal `RpcIO` module now provides bounded native reads and partial writes
over borrowed POSIX descriptors. Its C NIF duplicates descriptors with close-on-exec,
sets nonblocking mode, and restores public status flags when closing its copies.
Only the creating process may operate on a handle. A native owner monitor also
closes it if that process exits, even while another process retains the resource.
Broken-pipe writes preserve the VM and the calling thread's previous signal mask.

A scoped SIGINT resource records interrupts in a lock-free flag. Only one owner
may install it; explicit close and owner exit restore the previous signal action.
The bridge contains descriptor and signal operations. Agent execution, session
history, and parsed commands remain Elixir. JSONL framing, the bounded queue,
the connection driver, and end-to-end RPC shutdown are still required.

A probe of BEAM's built-in descriptor port did not meet shutdown requirements:
closing a port with stalled output could leave the VM waiting, and one forced
close variant crashed its subprocess. That probe was not adopted. The new bridge
returns partial-write progress directly and owns no asynchronous output queue.

`native.exs` builds the bridge into the application's `priv` directory through
a native Mix compiler task or the direct check helper. Compilation writes a
unique temporary output and atomically replaces the library, preserving any
mapped previous binary. macOS and Linux builds require a C11 compiler and Erlang
NIF headers; the rest of the Elixir SDK remains buildable on other platforms
without this bridge. Only macOS has been verified. Native hot upgrades are
explicitly rejected until resource takeover is implemented.

| Check | Result |
| --- | --- |
| Native compilation and analysis | Fourteen Elixir runtime sources pass native formatting and compilation. The C bridge compiles with `-Wall -Wextra -Werror -Wpedantic`; Clang static analysis reports no findings. No C formatter was available on PATH. |
| Offline checks | All sixty-one tests pass against the extracted package: the previous fifty-three core/provider/session tests plus eight native I/O subprocess checks. The new checks verify a 2 MiB binary pipe round trip, regular files, EOF, partial and stalled output, broken pipes, invalid/foreign-owner operations, retained-resource owner cleanup, SIGINT delivery, and prior-handler restoration. |
| Test corrections | The concurrent core test now observes provider entry after its expected text event, so cancellation cannot race that event. File checks compare public status flags; macOS separately records successful writes with its internal `FWASWRITTEN` flag, as defined in [XNU](https://github.com/apple/darwin-xnu/blob/main/bsd/sys/fcntl.h). |
| Native Hex package | `mix hex.build` succeeds. All twenty source-package files match current source: fourteen Elixir runtime files, C source, native build helper, manifest, lock, README, and license. Tests, CLI source, and prebuilt native binaries are excluded. |
| Isolated package and examples | Copied checks and fixtures pass all sixty-one offline tests. All twenty-seven loaded runtime modules match package source hashes, BEAM paths, and code MD5 values before and after the suite. All four README Elixir blocks parse, all three runnable examples pass, and credential-free provider/agent construction starts no CLI. The package's compiled native library remains unchanged through the consumers. |
| Full conformance attempt | Both mandatory TCP tests still fail at denied listener creation. The CLI self-test still fails at Mix's TCP filesystem lock. The attempt also exposed the two test issues above; their corrections pass in the final package suite. |
| Repository check | Root `npm run check` stops before source checks at the same three unrelated nested Biome roots. The native Python subprocess helper passes Black. |

Temporary probes, formatter, and package consumers were removed after verification.
Native RPC framing, its bounded queue and connection driver, validated tools and
their loop, coding-CLI integration, HTTP, Linux bridge acceptance, normal Mix
compilation, TCP, the Elixir 1.15 lower boundary, and final acceptance remain open.
No paid provider request, registry publication, commit, or push occurred.
The full eleven-language finish contract remains active.

## 2026-09-08 Elixir native RPC checkpoint

`Rpc.run_stdio` and `Rpc.run` now serve JSONL through the existing native core
and session controller. The driver owns connection lifetime; `RpcInput` owns
bounded UTF-8 framing; `RpcOutput` serializes partial native writes and owns the
queue; `RpcProtocol` holds encoding and limits. The C bridge remains limited to
descriptor and signal operations. The coding CLI still uses its original loop.

Commands remain readable while replies wait for stdout. Run acknowledgements
and events await full-line delivery, and history commits only after terminal
delivery and worker exit. Queue accounting includes every LF and the full
in-progress frame. Oversized output cancels its run; command-response
cancellation happens before accepting the next command, preventing delayed
cancellation from reaching a later run. Queue overflow closes the connection
after an intact current line and bounded error report when output can drain.

EOF, handled SIGINT, external cancellation, and output failures cancel the active
run and wait for cooperative provider cleanup. Cancelled writes have one second
per frame to drain before failing the connection. Return joins the reader,
writer, session controller, and run. Owner exit releases their native resources.
Borrowed originals remain caller-owned, and the previous SIGINT handler is restored.

A subprocess check found that opening an invalid descriptor during linked writer
initialization could exit the caller. Descriptor opening now happens after the
writer starts; failure stops it normally and joins it before returning `RpcError`.
The first shutdown fixture also compared process links by list order. It now
compares the actual process set and still rejects leftover links or messages.

| Check | Result |
| --- | --- |
| Native formatting and compilation | Eighteen runtime sources and all Elixir test/check files pass on Elixir 1.20.4 / OTP 29 with warnings treated as errors. The native bridge builds with its existing strict C flags. |
| Offline suite | All eighty-four tests pass. Twenty-three new RPC subprocess checks cover shared commands, completion, rollback, busy/abort/reset, UTF-8 and framing, exact line and queue boundaries, oversized output, stalled/broken pipes, held acknowledgement and terminal delivery, held cleanup, EOF/SIGINT, external cancellation, invalid startup, and owner exit. |
| Native Hex package | `mix hex.build` succeeds. All twenty-four packaged source files match current source: eighteen Elixir runtime files, C source, native build helper, manifest, lock, README, and license. |
| Isolated package | Copied tests and shared fixtures pass all eighty-four offline tests against the extracted package. All thirty-two loaded runtime modules match installed source paths, BEAM paths, and code MD5 values before and after the suite. Packaged source hashes and the compiled native library remain unchanged through the consumers. |
| README and import | All five Elixir blocks parse; all four runnable examples pass. Provider examples replace only provider construction with the native Mint fixture and verify output, model, and response cleanup. Parsed-session and native-RPC state examples match the shared fixture. Both provider/agent constructors load without credentials or CLI output. |
| Full conformance | `python3 conformance/run.py elixir` passes eighty-four tests and fails the two mandatory TCP tests at listener creation with `:eperm`. The original CLI self-test still fails at Mix's TCP filesystem lock. The runner now correctly keeps executable tools, their loop, CLI integration, and HTTP incomplete. |
| Repository checks | The Python test helpers and conformance runner pass Black. Root `npm run check` stops before source checks at the same three unrelated nested Biome roots. The TinyAgent directory remains untracked, so `git diff --check` does not validate its contents. |

Temporary formatter and package consumers were removed after verification.
Validated tools and their loop, coding-CLI integration, HTTP, Linux bridge
acceptance, normal Mix compilation, real TCP, the Elixir 1.15 lower boundary,
and final acceptance remain open. No paid provider request, registry publication,
commit, or push occurred. The full eleven-language finish contract remains active.

## 2026-09-08 PHP session checkpoint

The native RPC checkpoint below supersedes this checkpoint's byte-transport gaps
and verification counts.

`RpcSession` now maps parsed commands onto the existing PHP core. `Session`
owns committed history, admission, cancellation, and one fiber; `SessionRun`
retains that fiber through acknowledgement, provider cleanup, and final delivery.
No provider or tool loop was duplicated. Native PHP JSON objects remain distinct
from arrays, and validation rejects foreign objects without executing serializers.

Prompt handling reserves the session before execution. The caller explicitly
uses `advance()` or `awaitIdle()` to drive its fiber. Acknowledgement finishes
before the core starts, and history commits only after successful terminal
delivery and fiber termination. Failed or cancelled runs preserve prior history.
Reset clears history, closes admission during cleanup, and waits for the old run.
Close also stops admission. Calls from the run's own callback request cleanup
without waiting on themselves. Waiters retain their observed run, so a later
prompt cannot extend an earlier wait.

This is the parsed adapter, not native RPC transport. The existing cURL client
still waits synchronously; the connection driver must add cooperative network
waits, JSONL framing, bounded output, and input/output scheduling. There is no
implicit event loop or cleanup on object disposal. Callers must close the session
and resume fibers that hold cleanup. The PHP [fiber contract](https://www.php.net/manual/en/language.fibers.php)
informs the documented advancement model.

Test review found that session or provider code could catch a failed assertion
inside a callback. The check helper now retains the first assertion failure and
the runner fails the group even when that exception was swallowed. This exposed
an older incorrect assertion: cancelling a callback token cancels the run's
provider token. The corrected check also proves that the caller's parent token
remains uncancelled. No production cancellation behavior changed for that fix.

| Check | Result |
| --- | --- |
| Native syntax and CLI | All runtime sources, tests, check helpers, and the original CLI pass PHP syntax checks on PHP 8.4.3. The original CLI blacklist self-test passes. No PHP formatter is installed; changed files have no trailing whitespace or tabs. |
| Offline checks | All thirty-nine groups pass: the previous twenty-six core/provider groups plus thirteen session groups. They cover shared parsed commands, strict JSON values, both native provider parsers, acknowledgement order, history commit/rollback, busy/abort/reset, failed consumers, pending acknowledgement, held cleanup and final delivery, callback cleanup, independent sessions, and observed-run waiters. |
| Native package | `package.php` creates the ZIP. Its nineteen files match current source: sixteen runtime PHP files including the autoloader, plus the manifest, README, and license. Tests and CLI source remain excluded. |
| Isolated package | Copied checks and shared fixtures pass all thirty-nine groups against the extracted ZIP. All sixteen runtime files are loaded from the package and retain their source hashes. Reflection verifies the package paths of all thirty-seven loaded SDK classes/interfaces before and after the suite. |
| README and import | All three original PHP blocks pass syntax checks and run against the package. Provider examples replace only provider construction with the byte fixture and verify text, model, and cleanup. The session state example matches the shared RPC fixture. Provider, agent, and session construction and close need no credentials or CLI output. |
| Full conformance | `python3 conformance/run.py php` passes thirty-nine groups; both mandatory native cURL/TCP groups fail at denied listener creation. The runner keeps validated tools, their loop, CLI integration, RPC byte transport, and HTTP incomplete. |
| Repository checks | The conformance runner passes Black. Root `npm run check` stops before source checks at the same three unrelated nested Biome roots. TinyAgent remains untracked. |

Temporary package consumers were removed after verification. Native RPC with
cooperative cURL scheduling, validated tools and their loop, coding-CLI integration,
HTTP, real cURL/TCP acceptance, Composer installation, and the final audit remain
open. No paid provider request, registry publication, commit, or push occurred.
The full eleven-language finish contract remains active.

## 2026-09-08 PHP native RPC checkpoint

`Rpc::runStdio` and `Rpc::run` now drive the existing session through native
POSIX streams. Input, output, stream ownership, protocol framing, and process
signals have separate modules. No agent loop was duplicated. The driver owns
its streams, waits for cooperative provider cleanup, and restores scoped signal
handlers. PCNTL is an optional component dependency required by native RPC.

Input remains responsive while event delivery waits on a bounded output queue.
Complete frames, including a partially written frame, count against the 4 MiB
limit. Oversized events and replies cancel their run; overflow terminates the
connection. Cancelled output receives a one-second drain deadline. History
commits only after complete terminal delivery and run-fiber termination.

SDK-owned session fibers yield during cURL network waits. A native cURL file
transfer proves that its C callback can suspend and resume; this does not verify
the SDK HTTP client over TCP. Native socket writes bypass STDIO buffering, as
confirmed in [PHP's socket implementation](https://github.com/php/php-src/blob/PHP-8.4/main/streams/xp_socket.c).
The transport now configures those stream types separately.

| Check | Result |
| --- | --- |
| Native syntax, original CLI, and offline checks | `php check.php --offline` passes syntax checks, package creation, all fifty-five groups, and the original blacklist self-test on PHP 8.4.3. No PHP formatter is installed; changed PHP files have no tabs or trailing whitespace. |
| RPC coverage | Sixteen added groups cover subprocess JSONL, both provider byte fixtures, history, busy/reset/abort, EOF/SIGINT, broken and stalled native pipes, input/output limits, exact queue-byte accounting, partial-line preservation, held provider cleanup, caller-fiber interruption, independent connections, stream ownership, scoped signals, and cURL callback suspension. |
| Isolated package | The ZIP contains twenty-six files: twenty-three runtime PHP files and three metadata files. Copied checks pass all fifty-five groups against the extracted package. Runtime hashes and reflection paths verify all twenty-three files and forty-six SDK classes/interfaces before and after the suite. |
| README examples | All four original PHP blocks pass syntax checks and run against the package. Provider construction alone is replaced with the byte fixture in the two provider examples; both state examples run without credentials and match the shared protocol. |
| Full conformance | `python3 conformance/run.py php` passes fifty-five groups; both mandatory native cURL/TCP groups fail at listener creation with `Operation not permitted`. The runner still reports tools, their loop, CLI integration, and HTTP incomplete. |
| Repository check | The conformance runner passes Black. Root `npm run check` stops before source checks at the same three unrelated nested Biome roots. |

Native cURL network scheduling still needs TCP acceptance. Linux, Composer,
validated tools and their loop, coding-CLI integration, HTTP, and the final audit
remain open. Temporary package consumers were removed after verification. No
paid provider request, publication, commit, or push occurred. The finish contract
remains active.

## 2026-09-08 C# HTTP checkpoint

`TinyAgent.Http` now exposes `HttpServer.StartAsync(agent, options)`, the bound
`Address`, `RequestStop()`, and asynchronous disposal. It uses Kestrel directly
from the ASP.NET Core shared runtime, without an application host, dependency
injection container, process signal handlers, or another agent loop. The core
NuGet package retains no ASP.NET Core framework requirement. HTTP request
validation, route/session ownership, and streamed responses have separate modules.

All five routes are implemented with native HTTP/1 parsing, strict UTF-8 JSON,
1 MiB request/output limits, loopback defaults, explicit remote opt-in, bearer
authentication, and absent CORS. RPC and HTTP share the core session owner.
Disconnect callbacks cancel their exact run ID. Failed delivery and cancellation
retain prior history; deletion remains owned until its run finishes, including
when shutdown starts concurrently.

The adapter observes both request and connection cancellation. Kestrel suppresses
the request token during final response completion, so the connection token is
also checked around delivery. The native fixture injects a failed final HTTP
chunk and verifies rollback. Provider callback cancellation releases blocked
writes. Cancelled output and transport shutdown receive a one-second drain
deadline; asynchronous disposal still joins cooperative provider cleanup after
the connection is aborted. Provider callbacks can request shutdown without
awaiting their own return.

| Check | Result |
| --- | --- |
| Native build and offline checks | `python3 check.py --offline` builds core, HTTP, checks, and the original CLI with zero warnings/errors; packages both libraries; passes all sixty-eight groups and the CLI blacklist self-test. Verified with .NET SDK 8.0.303 and .NET/ASP.NET Core runtime 8.0.7 on macOS. |
| HTTP checks | Twelve groups use real Kestrel and SocketsHttpHandler over bounded in-memory pipes. They cover all routes, independent concurrent sessions, ordered JSONL, completed history, busy/cancel/delete, auth/exposure, malformed JSON/UTF-8/HTTP framing, fixed and chunked body limits, oversized events, disconnect cleanup, failed final chunks, provider cancellation during blocked delivery, callback-requested shutdown, and cleanup retained beyond transport cancellation. |
| Isolated NuGet packages | Both archives contain only their own runtime DLL plus package metadata, README, and license. Those files match the source build. A copied suite installs solely from the two-package local feed with a fresh package cache and passes all sixty-eight groups, including subprocesses. Consumer DLL hashes match the packages. |
| README examples | All five unchanged snippets compile against the packages. The two provider examples execute with only provider construction replaced by the native byte fixture; both RPC examples execute unchanged without credentials. The HTTP example executes with the in-memory listener substituted for TCP. Core-only examples have no ASP.NET Core runtime requirement. Native TCP example execution remains unverified. |
| Full conformance | `python3 conformance/run.py csharp` passes sixty-eight groups. All three mandatory TCP groups fail at denied local binding with `SocketException (13): Permission denied`. The original CLI self-test passes. The runner now reports executable tools/their loop and coding-CLI integration as unimplemented. |
| Formatting and repository check | Native .NET whitespace verification passes for the changed C# files. Black passes for both Python runners. Root `npm run check` stops before source checks at the same three unrelated nested Biome roots. |

The in-memory fixtures do not establish TCP behavior. Tools, Draft 2020-12
argument validation, coding-CLI integration, TCP/platform acceptance, and the
final audit remain open. No paid provider request, publication, commit, or push
occurred. The finish contract remains active.

## 2026-09-08 C# Bash execution checkpoint

The SDK now exposes `Bash.RunAsync(command, cancellationToken)` and the shared
`Bash.DeniedReason` blacklist. The coding CLI uses this executor instead of its
previous unbounded subprocess capture. Standard output and error share a
131072-byte limit, including the final observation envelope. Overflow stops the
command; the CLI turns that failure into an observation the model can recover
from. Nonzero exits retain the existing observation format. Cancellation joins
process cleanup before raising `OperationCanceledException`.

A short Bash supervisor uses native
[job control](https://www.gnu.org/software/bash/manual/html_node/Job-Control-Basics.html)
to give the command a separate process group. It handles cancellation while
waiting, kills remaining group members on exit, and hides its own job-status
messages. Commands receive closed stdin. Ambient Bash startup files/options
cannot replace the supervisor's traps. This is process cleanup, not a sandbox;
deliberately detached processes remain outside that group.

The CLI passes run cancellation through provider requests and Bash. A turn
returns a new history only after completion; failed and cancelled turns leave
prior history unchanged. The existing complete-response CLI loop remains until
schema-validated tools can be integrated with the native `Agent`.

| Check | Result |
| --- | --- |
| Native build and offline checks | `python3 check.py --offline` passes seventy-six groups and the CLI blacklist self-test, with zero compiler warnings/errors. Verified on .NET 8.0.7 and macOS Bash 3.2.57. |
| Bash coverage | Five groups verify blacklist preservation, bounded denial messages, Unicode/NUL/empty input, stdout/stderr and exit status, closed stdin, invalid output-byte replacement, exact combined byte bounds, infinite-output cancellation, process-group cancellation, independent commands, and background cleanup after normal completion. Native signal checks confirm the recorded child PID no longer exists. |
| CLI coverage | Three groups compile the same CLI source into the native check host and inject only HttpClient configuration. They verify native Bash observations and completed history, recovery from output overflow, cancellation during provider headers and Bash, and unchanged prior history. The interactive Console Ctrl-C event itself was not exercised end to end in these groups. |
| Isolated packages and examples | Both NuGet archives match their built DLLs, README, and license. All seventy-six copied checks pass against a fresh local package feed/cache, including RPC subprocesses and the CLI source. All six unchanged README snippets compile. Both RPC snippets and the Bash snippet execute unchanged; provider and HTTP snippets retain the previously documented fixture substitutions. |
| Full conformance | `python3 conformance/run.py csharp` passes seventy-six groups and the CLI self-test. All three mandatory TCP groups fail at denied local binding. Schema-validated tools, their loop, and full CLI integration remain incomplete. |
| Formatting and repository check | Native .NET whitespace verification passes for changed C# sources. Root `npm run check` still stops at the three unrelated nested Biome root configurations. |

No JSON Schema validator is cached locally, and shell access to NuGet fails DNS
resolution. The official JsonSchema.Net package page is reachable in the browser,
but browser security review rejected its download, reporting declined permission.
The download was not retried through another route, and no dependency was added.
Schema validation remains an external dependency gap. Linux behavior, native TCP,
registered tool execution, CLI integration with `Agent`, and the final audit
remain open. Temporary probes and package consumers were removed after
verification. No paid provider request, publication, commit, or push occurred.

## 2026-09-08 Java Bash execution checkpoint

The SDK now exposes `Bash.run(command, cancellation)` and `Bash.deniedReason`.
The original coding CLI imports this executor from the SDK JAR. Standard output
and error share a 131072-byte limit, including the observation envelope.
Overflow stops the command and raises `IOException`; the CLI turns execution
errors into bounded observations and lets the model recover. Nonzero exits and
blacklist denials retain their observation format.

A short Bash supervisor assigns the command its own process group using job
control. It forwards cancellation to that group and removes remaining background
members after normal exit. The command receives closed stdin; supervisor job
messages remain hidden. Ambient Bash startup options cannot replace its traps.
The Java caller joins process and native reader-thread cleanup before returning
or throwing. Caller interruption retains the interrupt flag after cleanup.
This is process ownership, not a sandbox; deliberately detached groups remain
outside this boundary.

The CLI now cancels pending asynchronous HTTP requests and Bash through the
same run token. A shutdown hook exists only during an active turn and waits for
its cleanup. Completed turns return new history; provider failures and
cancellation preserve prior history. The complete-response CLI loop remains
until schema-validated tools can be integrated with the native `Agent`.

| Check | Result |
| --- | --- |
| Native compilation and checks | Java 21 compilation on OpenJDK 25.0.1 passes with `-Xlint:all -Werror`. Fifty-five SDK groups, four CLI groups, and the source-entrypoint blacklist self-test pass offline. |
| Bash coverage | Six groups verify blacklist preservation, bounded denial text, empty/NUL/invalid Unicode input, stdout/stderr and status, closed stdin, invalid output-byte replacement, exact combined byte limits, infinite output, run cancellation, caller interruption, independent commands, and background cleanup after normal completion. Recorded child PIDs disappear after cleanup. |
| CLI coverage | Four groups compile the original CLI against the packaged executor. Fixture HTTP responses verify tool observations, recovery from output overflow, completed history, rollback on provider errors, and cancellation during headers and Bash. A real JVM subprocess receives termination and exits after its Bash child disappears. This shutdown check uses SIGTERM, not a terminal-generated Ctrl-C. |
| Isolated JAR | The runtime JAR's 53 class files, README, and license match the build inputs. All fifty-five copied SDK groups and four CLI groups pass with only copied JARs, tests, and fixtures on the classpath. The original source CLI self-test needs only the SDK JAR, without Jackson. |
| README examples | All three unchanged Java snippets compile against the isolated package. Bash and idle RPC execute unchanged; the provider example executes with only its provider constructor replaced by the native fixture client and returns two committed messages. |
| Full conformance | `python3 conformance/run.py java` runs the same fifty-nine passing groups and CLI self-test. Both mandatory TCP groups fail at denied local socket binding. The runner reports registered tools/their loop, CLI integration with `Agent`, and HTTP as incomplete. |
| Formatting and repository check | `black --check tinyagents/java/check.py conformance/run.py` passes. Root `npm run check` stops at the same three unrelated nested Biome root configurations. |

The Java CLI now requires `-cp target/tiny-agent-0.1.0.jar`; all current command
examples use that classpath. Its separate CLI checks live in `src/test/cli` and
are compiled by `check.py`, so Maven's SDK test compilation does not depend on
the default-package CLI source. Maven packaging remains unverified because its
plugin dependencies are not cached and registry DNS is unavailable. Linux Bash,
real TCP behavior, the JSON Schema dependency, registered tool execution, full
CLI integration, HTTP, and the final audit remain open. No dependency download,
paid provider request, publication, commit, or push occurred.

## 2026-09-08 Ruby Bash execution checkpoint

The SDK now exposes `TinyAgent::Bash.run` and `Bash.denied_reason`. The original
Ruby CLI imports the same executor. Output uses separate stdout and stderr
fields with a combined 131072-byte limit, including the observation envelope.
Oversized output stops execution and raises `IOError`; the CLI turns execution
errors into bounded observations and lets the model recover. Nonzero exits and
blacklist denials retain their existing observation format.

Ruby reads native pipes on the calling thread and polls cancellation while
waiting. A small Bash supervisor owns the command's process group, stops it on
cancellation, and removes remaining background members after normal completion.
The caller joins the supervisor and closes every pipe before returning or
raising. Asynchronous Ruby exceptions are deferred during resource acquisition
and cleanup. No Ruby worker thread is created by the executor. Deliberately
detached groups remain outside this ownership boundary; the blacklist is not
a sandbox.

The CLI passes the run's token through HTTP and Bash and returns a new history
only after successful completion. Provider failures and cancellation preserve
the previous history. Importing the CLI defines its module without starting
the input loop or requiring credentials. It handles Ruby's default Ctrl-C
exception after native cleanup, without installing a signal handler. The
complete-response loop remains until registered tools can use the native `Agent`.

| Check | Result |
| --- | --- |
| Native offline checks | `ruby check.rb --offline` passes sixty-two groups and 688 assertions on Ruby 3.1.0/macOS. Changed runtime, CLI, dispatcher, and fixture files pass `ruby -wc`. No Ruby formatter is installed. |
| Bash coverage | Six groups cover shared guards, bounded denial messages, Unicode/NUL/empty input, stdout/stderr and status, closed stdin, invalid output-byte replacement, exact combined bounds, infinite output, cancellation, caller interruption, independent commands, and background cleanup after normal completion. Recorded child PIDs disappear after cleanup. |
| Cancellation stress | Direct Ruby group signalling produced `EPERM` during overflow cleanup. The final supervisor keeps Ruby cancellation addressed to an owned, unreaped process. Sixty consecutive threaded overflow checks pass with that implementation. |
| CLI coverage | Four groups exercise the original CLI with native Net::HTTP framing fixtures. They verify shared Bash observations, overflow recovery, completed history, provider-error rollback, cancellation during headers/body/Bash, and a real CLI subprocess receiving SIGINT. The CLI exits successfully after its Bash child disappears. |
| Isolated gem | All fourteen packaged files match source, including twelve runtime files, README, and license. All sixty-two copied groups and the CLI self-test pass against the installed gem. An import assertion checks SDK file locations in both the test process and its native subprocesses. RubyGems retains its existing optional-homepage warning. |
| README examples | All four unchanged snippets pass syntax checks. Bash and idle RPC execute unchanged against the installed gem. Agent and direct-provider examples execute with only provider construction replaced by the local fixture; native HTTP cleanup is verified. |
| Full conformance | `python3 conformance/run.py ruby` passes sixty-two groups and the CLI self-test. Both mandatory TCP groups fail at denied local binding. The runner still reports registered tools/their loop, CLI integration with `Agent`, and HTTP as incomplete. |
| Repository check | Root `npm run check` stops at the same three unrelated nested Biome root configurations. |

Schema validation still needs its native dependency. Registered tool execution,
the shared agent loop in the CLI, HTTP, Linux process behavior, real TCP
acceptance, and the final audit remain open. Scratch reproductions and isolated
package consumers were removed after verification. No dependency download,
paid provider request, publication, commit, or push occurred.

## 2026-09-08 PHP Bash execution checkpoint

The SDK now exposes `Bash::run` and `Bash::deniedReason`. The original PHP CLI
imports the same executor and the SDK's cancellable `CurlClient`. The CLI
retains its complete-response loop until registered tools can use `Agent`.
HTTP response bodies are bounded to 1 MiB; successful turns return new history,
while request failures and cancellation preserve the previous transcript.
The HTTP client accepts an explicit `Accept` header and retains its SSE default
for existing streaming providers.

Bash uses nonblocking pipes and a supervisor that owns the command's process
group. Standard output and error share a 131072-byte limit, including the
observation envelope. Overflow stops the command; the CLI converts execution
errors into bounded, recoverable observations. Cancellation joins the supervisor
and closes every pipe before raising `Cancelled`. Background group members are
removed after normal completion; deliberately detached groups remain outside
this boundary. The blacklist is not a sandbox.

SDK-owned fibers yield during reads and process cleanup. Direct calls and
application-created fibers remain synchronous. Throwing into an owned fiber
preserves the first exception and continues cleanup even if another exception
arrives during a later suspension. Callers must resume suspended fibers until
cleanup finishes. PHP 8.4 supplies the
[cached process status](https://www.php.net/manual/en/function.proc-get-status.php)
needed to retain exit codes while draining output.

The CLI requires PCNTL for scoped SIGINT handling. Native checks reproduced an
idle prompt remaining blocked in `fgets()` after SIGINT; prompt reads now use
nonblocking polling. The CLI restores its previous stdin blocking state,
SIGINT handler, and asynchronous signal-dispatch setting before returning.
Importing the CLI defines its functions without starting an input loop or request.

| Check | Result |
| --- | --- |
| Native syntax and checks | PHP 8.4.3 syntax checks pass for runtime, helpers, tests, and CLI. Sixty-six offline groups pass, plus the original CLI blacklist self-test. No PHP formatter or static analyzer is available on PATH; changed sources have no trailing whitespace. |
| Bash coverage | Seven groups cover guards and input validation, bounded denial text, native stdout/stderr/status, closed stdin, invalid output-byte replacement, exact combined bounds, infinite output, cancellation, thrown fiber exceptions, independent commands, background cleanup, and scheduling scope. Native POSIX checks confirm recorded child PIDs disappear. |
| CLI coverage | Four groups verify shared Bash observations, overflow recovery, provider failure, response bounds, history rollback, request/cleanup/Bash cancellation, idle and active SIGINT, signal/input restoration, and conversation history across EOF with an unterminated final input line. Request fixtures inject bytes and do not prove native cURL or HTTP framing. |
| Isolated ZIP | All twenty-seven archive entries match source, including twenty-four runtime PHP files and three metadata files. All sixty-six copied groups and the CLI self-test pass against the extracted package. Runtime hashes and reflection paths verify all twenty-four files and forty-seven SDK classes/interfaces before and after checks, including subprocesses. |
| README examples | All five unchanged snippets pass PHP syntax checks. Bash and both state examples execute unchanged against the package. The two provider examples replace only provider construction with the byte fixture and verify expected text and response cleanup. |
| Full conformance | `python3 conformance/run.py php` passes syntax, packaging, sixty-six groups, and the CLI self-test. Both mandatory native cURL/TCP groups fail at denied local binding. The runner keeps registered tools/their loop, CLI integration with `Agent`, and HTTP incomplete. |
| Repository check | Root `npm run check` stops at the same three unrelated nested Biome root configurations. |

A shell startup briefly reported no free disk space. A subsequent check showed
427 MiB available, and the small package validation completed. No unrelated
files or caches were removed. Temporary package consumers were removed after
verification. Native cURL/TCP, Linux, Composer, schema-validated tools, the shared
CLI agent loop, HTTP, and the final audit remain open. No dependency download,
paid provider request, publication, commit, or push occurred.

## 2026-09-08 Swift Bash execution checkpoint

The SDK now exposes macOS `Bash.run` and `Bash.deniedReason`. The coding CLI
imports the same executor and OpenAI streaming provider, uses typed neutral
history and replay, and no longer contains untyped JSON or duplicate HTTP code.
It still owns its tool loop until schema-validated handlers can run through
`Agent`. Successful turns return new history. Failed or cancelled turns retain
the previous transcript.

Bash uses nonblocking stdout/stderr pipes and a supervisor that owns a separate
process group. Output has a shared 131072-byte UTF-8 limit, including the final
observation envelope and replacement of invalid bytes. Overflow stops the
command and raises an error; the CLI converts ordinary tool errors into bounded
observations. Task cancellation stops the group and awaits process cleanup
before raising `CancellationError`. Normal exit removes background group members.
Deliberately escaped groups remain outside cleanup, and the blacklist is not
a sandbox. The SDK executor installs no signal handler.

Native checks exposed a blocked `Process.waitUntilExit()` call even after
`isRunning` became false. Cleanup now awaits Foundation's termination callback.
An owned task shields this wait from the caller's cancellation. A separate CLI
check reproduced Ctrl-C being lost between the welcome message and signal
installation. The CLI now installs its handler before output and retains an
interrupt until its owned task is attached. Exit restores stdin flags and the
previous SIGINT action. Nonblocking prompt input accepts EOF without a final LF.

| Check | Result |
| --- | --- |
| Native compile and offline checks | Sixty-nine groups pass: the previous fifty-eight groups, six Bash groups, and five CLI groups. The compiled CLI self-test passes. Swift 6 compilation treats warnings as errors; strict native formatting passes. |
| Bash coverage | Guards, command validation, bounded denial text, separate streams/status, stdin EOF, invalid UTF-8, shared raw and envelope bounds, infinite output, cancellation, independent calls, and normal/background cleanup pass. Native PID checks confirm that cancelled commands and their children disappear. |
| CLI coverage | Shared Bash output, recoverable invalid arguments and overflow, typed replay, native URLSession streaming, provider and callback failures, history rollback, request/Bash cancellation, conversation EOF, idle/active Ctrl-C, and restored signal/input settings pass. |
| Isolated source package | All twenty-one archive files match source, including seventeen runtime Swift files and four metadata files. All sixty-nine groups and the copied CLI self-test pass using the extracted SDK. The build helper verifies the loaded library's absolute path and SHA-256 digest in checks and native fixture subprocesses. |
| README examples | All five unchanged Swift snippets compile against the extracted library. Bash and both state examples execute unchanged. Both provider examples replace provider construction with the local URLProtocol fixture and verify UTF-8 text and fixture cleanup. |
| Regression probes | Fifty short process exits and ten Ctrl-C signals sent immediately after the first visible CLI byte pass against the isolated package. Standard input remains open during the interrupt probes. |
| Apple targets | The library and CLI compile for macOS 13. All seventeen SDK files pass an iOS 16 simulator type check; Bash is conditionally excluded there. SwiftPM acceptance remains open under the previously recorded manifest-sandbox restriction. |
| Full conformance | `python3 conformance/run.py swift` passes compilation, formatting, packaging, sixty-nine groups, and the CLI self-test. Both mandatory TCP groups fail at local bind with `Operation not permitted`. The runner retains executable tools/their loop, CLI integration with Agent, and HTTP as incomplete. |
| Repository check | Root `npm run check` stops at the same three unrelated nested Biome root configurations. |

One existing native RPC queue-overflow check failed during the initial
investigation, then passed on subsequent source and isolated-package runs without
an RPC runtime change. The final acceptance audit must retain transport timing
coverage. No tests were weakened or removed.

Temporary consumers and regression probes were removed after verification.
Schema-validated tools and their shared agent loop, CLI integration with `Agent`,
HTTP, native TCP, SwiftPM acceptance, and the final audit remain open. No
dependency download, paid provider request, publication, commit, or push occurred.

## 2026-09-08 Elixir Bash execution checkpoint

The SDK now exposes `Bash.run` and `Bash.denied_reason`. The coding CLI shares
that executor, the SDK's OpenAI streaming provider, and typed neutral history
with provider replay. `cli.ex` owns the console and temporary tool loop;
`tiny_agent.exs` remains the executable entrypoint and loads the local SDK.
There is no duplicate Req-based HTTP client or synchronous `System.cmd` Bash
runner. Schema-validated executable tools and integration with `Agent` remain
open.

A native Erlang port helper captures stdout and stderr separately and bounds
their combined raw bytes before sending results to the VM. Elixir validates the
final UTF-8 observation against the same 131072-byte limit, including its
envelope and invalid-byte replacement. Ordinary Bash failures become bounded,
recoverable CLI observations. Cancellation preserves its exception and waits
for native cleanup. Calls preserve unrelated messages and `trap_exit` settings.

The initial direct process-group implementation reproduced `EPERM` when it
signaled an exited group. The final helper owns a live Bash supervisor instead.
The supervisor owns job-control cleanup; the helper signals and waits for only
its own child PID. That PID remains unreaped until cleanup is complete. Normal
exit removes background group members. Cancellation and port-owner exit also
stop the command; a forcibly terminated owner cannot itself await cleanup.
Deliberately escaped groups remain outside this boundary. The blacklist is not
a sandbox.

The console reuses the existing POSIX descriptor bridge. An owned task runs each
turn while its caller watches SIGINT. Native reads and writes remain cooperative,
including when stdout stops draining. Ctrl-C cancels an idle prompt, request,
Bash command, or output callback and waits for cleanup. Signal handling and
borrowed descriptor flags are restored before return. Successful turns commit
history; failures preserve prior history. EOF accepts a final unterminated line.

| Check | Result |
| --- | --- |
| Native compile and offline checks | All 106 tests pass: the previous 84 tests, eleven Bash tests, and eleven CLI tests. SDK and CLI compile without warnings on Elixir 1.20.4 and OTP 29; strict formatting passes. Both native C files compile with C11 warnings treated as errors. |
| Bash coverage | Validation, bounded blacklist output, stdout/stderr/status, stdin EOF, invalid UTF-8, combined raw and envelope bounds, infinite output, pre-cancellation, caller messages/settings, independent calls, background cleanup, cancellation, owner exit, and removed startup environment pass. Native checks verify recorded command and child PIDs disappear. |
| CLI coverage | Native Mint streaming, advertised Bash schema, sequential observations, typed history/replay, recoverable errors, original failures, request cancellation, history rollback, EOF, restored flags and SIGINT, idle/request/Bash interrupts, stalled writes, and broken output pass. |
| Native memory checks | Clang static analysis reports no findings in the new port helper. AddressSanitizer and UndefinedBehaviorSanitizer pass six probes covering separate output/status, invalid bytes, the raw byte bound, infinite-output overflow, cancellation, and owner-pipe closure. |
| Isolated Hex package | All 26 source files match the package manifest: nineteen Elixir runtime files, two C files, the native build helper, and four metadata files. All 106 copied tests and the copied CLI entrypoint self-test pass against the extracted package. A test launcher verifies all 33 SDK module paths and hashes plus both native binary hashes before/after parent checks and in native fixture subprocesses. |
| README examples | All six Elixir snippets parse. The dependency declaration is not a runnable example. All five runnable examples execute against the package. Bash and both state examples are unchanged; both provider examples replace construction with the local Mint fixture and verify text and closed connections. |
| Full conformance | With the already compiled locked dependencies, `python3 conformance/run.py elixir` passes compilation, 106 tests, Hex packaging, and the original CLI self-test. Both mandatory TCP tests fail at local listener creation with `:eperm`. The runner retains executable tools/their loop, Agent CLI integration, and HTTP as incomplete. |
| Repository checks | Black passes for the changed Python files. Root `npm run check` stops at the same three unrelated nested Biome root configurations. |

The CLI no longer installs Req independently. Its normal Mix installation path,
Linux execution, Elixir 1.15 lower boundary, native TCP, schema-validated tools,
the shared Agent tool loop, HTTP, and the final acceptance audit remain open.
Temporary consumers and native probes were removed after verification. No
dependency download, paid provider request, publication, commit, or push occurred.

## 2026-09-08 Java streaming CLI checkpoint

The coding CLI now uses the SDK's OpenAI streaming provider, immutable neutral
history, replay metadata, and guarded Bash executor. Its duplicate HTTP request
code and handwritten JSON parser are removed. Every returned tool call executes
sequentially. Invalid Bash arguments and ordinary execution errors become
recoverable observations. Provider failures, cancellation, and failed output
preserve prior history. Text streams as it arrives; a final answer supplied
without deltas is still displayed.

Native console writes use interruptible JDK channels. A turn-scoped shutdown
hook cancels requests and Bash work, closes blocked output, and waits for turn
cleanup before JVM exit. Idle input holds no hook. The console owns its standard
descriptors. The CLI still owns its temporary tool loop because registered,
schema-validated executable tools are not yet implemented in `Agent`.

| Check | Result |
| --- | --- |
| Native compile and checks | Java 21 compilation on OpenJDK 25.0.1 passes with warnings treated as errors. All 55 SDK groups, eleven CLI groups, and the source-entrypoint self-test pass offline. |
| CLI behavior | Real SDK SSE parsing verifies streamed and terminal-only text, request metadata, opaque replay, sequential calls, argument errors, overflow recovery, typed history, malformed/failed providers, failed text and diagnostic callbacks, terminal cancellation, and headers/body/Bash cancellation. Console checks cover blank input, failed-turn rollback, and unterminated EOF. |
| Native shutdown | Actual Ctrl-C tests cover idle input, pending provider work, active Bash, and a full stdout pipe. A broken stdout test also passes. Provider cleanup markers are written before JVM exit; recorded Bash child PIDs disappear before the test completes. |
| Isolated package | All 53 SDK runtime classes load from the copied JAR. All 55 SDK groups and eleven CLI groups pass with only copied JARs, tests, the CLI, and shared fixtures. The documented source CLI command passes with copied dependencies. JAR SHA-256 is `92dc379e48639ffc0c9fadb69f68ec2628d95868983afdc01f35e397f0da5611`. |
| README examples | All three compile unchanged against the isolated package. Bash and idle RPC run unchanged; the agent example executes with a local SSE fixture substituted for provider construction. |
| Full conformance | `python3 conformance/run.py java` passes the 66 offline groups and self-test, then fails both mandatory TCP groups at denied native socket binding. The runner correctly keeps executable tools, Agent CLI integration, and HTTP incomplete. |
| Repository checks | Black passes for `check.py`. Root `npm run check` stops at the same three unrelated nested Biome root configurations. |

The build helper copies the three already declared Jackson JARs to
`target/dependency`; the CLI now includes those JARs in its documented classpath.
No dependency was added or downloaded. The earlier Java HTTP investigation still
applies: the JDK server lacks the required connection lifecycle, no suitable
server library is cached, and Maven Central DNS is unavailable. HTTP, tools and
the shared Agent loop, Maven packaging, native TCP, and final acceptance remain
open. No paid provider request, publication, commit, or push occurred.

## 2026-09-08 Ruby streaming CLI checkpoint

The coding CLI now uses `OpenAIProvider.stream`, `ToolDefinition`, frozen neutral
history, provider replay, and the existing Bash executor. Its duplicate
complete-response HTTP request code is removed. Text streams as it arrives;
final text without deltas is still displayed. All returned Bash calls execute
sequentially, with recoverable observations for invalid arguments and ordinary
execution errors. Failed or cancelled turns preserve prior history. Failed
callbacks retain their original exception through provider cleanup, and Ruby
nonlocal exits unwind without another callback.

Native console writes use `write_nonblock` and wait cooperatively for pipe
capacity. The CLI handles Ruby's default Ctrl-C exception after HTTP and Bash
cleanup, restores borrowed blocking modes, and leaves descriptors open. It
does not install a signal handler or write a final newline into a stalled pipe.
The temporary CLI tool loop remains separate until executable tools are
implemented in `Agent`.

An initial fixture compared every `F_GETFL` bit and failed after normal writes.
A standalone probe reproduced that difference without the CLI. It was Darwin's
kernel-maintained `FWASWRITTEN` bit, which `F_SETFL` does not restore. Tests now
check the caller's blocking mode, descriptor flags, and open ownership; both
initially blocking and nonblocking output are covered. The definitions and
settable-mask behavior are in Apple's [fcntl header](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/fcntl.h)
and [descriptor implementation](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_descrip.c).

| Check | Result |
| --- | --- |
| Native offline checks | `ruby check.rb --offline` passes all 69 groups and 783 assertions on Ruby 3.1.0. Changed Ruby files pass `ruby -wc`; the source CLI blacklist self-test passes. |
| CLI behavior | Eleven groups verify actual SDK SSE parsing, request metadata, streamed and terminal-only text, opaque replay, sequential calls, argument errors, overflow recovery, frozen history, malformed/failed providers, failed text and diagnostic callbacks, nonlocal exits, terminal cancellation, headers/body/Bash cancellation, console rollback, blank input, and unterminated EOF. |
| Native cleanup | Real subprocesses pass Ctrl-C checks during idle input, pending HTTP, active Bash, and a full stdout pipe. Broken stdout also exits. Cleanup records confirm closed HTTP streams, joined deadline threads, restored blocking modes and descriptor flags, and retained IO ownership; recorded Bash child PIDs disappear. |
| Isolated gem | All 14 package files match current source, including twelve runtime Ruby files. All 69 copied groups and 783 assertions pass against the installed gem. A preload guard checks all twelve loaded SDK paths and hashes before and after parent tests and native fixture subprocesses. The copied CLI self-test passes. Gem SHA-256 is `7e5af1d41ff9857fdb0dcf63f0a23df3a3dc13d21ffa468435d069bf2a0707b5`. |
| README examples | All four parse unchanged. Bash and idle RPC execute unchanged; the two provider examples run with local native HTTP/SSE fixtures substituted for provider construction. |
| Full conformance | `python3 conformance/run.py ruby` passes the 69 offline groups and source CLI self-test. Both mandatory TCP groups fail at denied native listener binding. Tools, Agent CLI integration, and HTTP remain incomplete in the runner. |
| Repository checks | Root `npm run check` stops at the same three unrelated nested Biome configurations. Gem building retains its existing missing-homepage warning. |

The runtime gem has no new dependency or runtime file. The installed consumer
and native probes were temporary. Schema validation, executable tools and the
shared Agent loop, HTTP, Linux/native TCP acceptance, and the final audit remain
open. No dependency download, paid request, publication, commit, or push occurred.

## 2026-09-08 C# streaming CLI checkpoint

The coding CLI now uses `OpenAIProvider.StreamAsync`, `ToolDefinition`, immutable
neutral history, provider replay, and the shared Bash executor. Text streams as
it arrives; terminal text without deltas is also displayed. Every returned Bash
call runs sequentially. Invalid arguments and ordinary execution failures become
bounded recoverable observations. Failed callbacks retain their original
exception through provider cleanup. Failed or cancelled turns leave prior
history unchanged, including cancellation during final text delivery.

Redirected Linux/macOS streams share RPC's native handle helper, renamed from
`RpcStdio` to `Stdio`. Its implementation is unchanged. The SDK grants internal
access to the first-party CLI and check assembly without adding a public API.
The CLI uses normal console input when attached to a terminal and owned native
handles for redirected pipes. Only an active turn intercepts Ctrl-C. It cancels
provider work, Bash, and output, joins cancellation callbacks, and returns 130
after cleanup. Idle Ctrl-C uses the default process behavior. The imported CLI
runner leaves caller-supplied streams and providers open.

| Check | Result |
| --- | --- |
| Native source checks | `python3 check.py --offline` passes all 84 groups and the CLI blacklist self-test with provider credentials removed. SDK 8.0.303 and .NET/ASP.NET Core 8.0.7 were used on macOS. Builds report zero warnings and errors; restore retains its nonfatal platform `CSSM_ModuleLoad` diagnostic. |
| CLI behavior | Eleven groups verify actual SDK HTTP/SSE parsing, streamed and terminal-only text, request metadata, opaque replay, sequential Bash calls, invalid arguments, overflow recovery, immutable history, provider and consumer failures, terminal cancellation, header/body cancellation, console rollback, blank input, and unterminated EOF. |
| Native cleanup | Real subprocesses verify Ctrl-C during idle input, pending HTTP, active Bash, and stalled stdout/stderr. Broken stdout and `/dev/null` redirection also pass. Cleanup records show no active provider request, closed HTTP streams, and disposed fixture providers. Recorded Bash child PIDs disappear. No PTY flow-control or Linux execution check was performed. |
| Isolated NuGet consumer | Both packages contain DLLs, README, and license matching current source. All 84 copied groups and the copied CLI self-test pass with package references restored only from an isolated local feed. Module initializers verify the loaded SDK/HTTP assembly paths and hashes in the consumer and native subprocesses. |
| README examples | All six compile unchanged. The two RPC examples and Bash example run unchanged. Agent and direct-provider examples run with native HTTP/SSE fixtures substituted for provider construction. The HTTP example runs with a bounded in-memory listener; its reported address correctly uses the fixture's port 12345. |
| Full conformance | `python3 conformance/run.py csharp` passes 84 groups and the CLI self-test. All three mandatory TCP groups fail at native listener binding with `SocketException (13): Permission denied`. The runner still reports executable tools, their loop, and Agent CLI integration as incomplete. |
| Formatting and repository check | Scoped `dotnet format whitespace --verify-no-changes` passes. Root `npm run check` stops at the same three unrelated nested Biome configurations. |

Verified package SHA-256 values:

- `TinyAgent.Sdk.0.0.1.nupkg`: `4ee834990ea682f27b46c1eb16c529f26ec52156db6f325a0fff6b6c5eea32e9`
- `TinyAgent.Http.0.0.1.nupkg`: `74ab9a25a89ceccf2648c271c1b55e08936593ff0d358c80606df5b0d0118c37`

The CLI tool loop remains separate until executable tools are implemented in
`Agent`. The pending authorization for the C# schema-validator download has
not been answered or bypassed. General schema validation, executable tools,
Agent CLI integration, native TCP, and final acceptance remain open. No new
dependency, paid request, publication, commit, or push occurred.

## 2026-09-08 PHP streaming CLI checkpoint

The coding CLI now uses the SDK's streaming `OpenAIProvider`, `ToolDefinition`,
neutral typed history, replay, and Bash executor. Its duplicate complete-response
request and parsing code is removed. Text streams as it arrives, with a fallback
for terminal-only text. Every returned Bash call executes sequentially. Invalid
arguments and ordinary execution errors become bounded recoverable observations.
Only a successful turn returns new history. Consumer failures retain their
original exception through provider wrapping or suppression. A closed request's
retained callback cannot deliver later text.

Console input and output use nonblocking native streams and cooperative waits.
Ctrl-C cancels idle input, requests, Bash, and stalled writes, then returns 130
after cleanup without writing into a stalled pipe. The CLI borrows its streams,
restores all three blocking modes, and restores SIGINT, shared SIGPIPE
suppression, asynchronous dispatch, and temporary error handlers. Callback
cancellation is scoped to the turn and does not cancel the caller's token.

Checks initially exposed two runtime gaps. A custom provider could cancel its
request token and return success; the CLI now checks that token before closing
its request scope. An exception thrown into a suspended fiber was incorrectly
converted into a Bash observation. `Cooperative` now identifies exceptions
arriving at SDK-owned suspension points using weak keys. The CLI preserves the
original interruption through provider wrapping, Bash cleanup, and console
cleanup. This follows PHP's documented [Fiber::throw behavior](https://www.php.net/manual/en/fiber.throw.php).
No exception objects are retained by that lookup.

| Check | Result |
| --- | --- |
| Native source and syntax | `php check.php --offline` passes native syntax, ZIP creation, 73 groups, and the CLI blacklist self-test on PHP 8.4.3. Final source checks also pass all 73 groups after tightening callback cancellation scope. No formatter or static analyzer is available on PATH; changed PHP files have no tabs or trailing whitespace. |
| CLI behavior | Eleven groups cover SDK SSE parsing, request metadata, streamed and terminal-only text, replay, sequential Bash calls, invalid arguments, overflow, provider failures, original consumer exceptions, immutable values and detached reference aliases, request/cleanup/Bash cancellation, fiber interruption, retained callbacks, conversation rollback, blank input, and unterminated EOF. |
| Native pipes and cleanup | Real subprocesses pass idle/request/Bash SIGINT, stalled stdout/stderr, initially nonblocking stdout, and broken stdout. Cleanup records verify closed responses, no active provider work, retained stream ownership, restored blocking modes and signals, and disappeared Bash child PIDs. Borrowed-file checks also verify caller error-handler restoration and interrupted console-fiber cleanup. |
| Terminal probes | A PTY conversation displays streamed UTF-8 text, reports a failed middle turn, and retains history counts `[1, 3, 3]`. Idle and active-Bash Ctrl-C both exit 130 with restored signals and streams. The recorded Bash child no longer exists. These probes ran on macOS; Linux and terminal flow-control backpressure are not accepted. |
| Isolated ZIP | All 27 entries match source: 24 runtime PHP files and three metadata files. All 73 copied groups and the copied CLI self-test pass. A PHP preload guard verifies loaded paths and SHA-256 values for all runtime files and 47 SDK classes/interfaces before and after the parent suite and native subprocesses; it separately verifies the copied CLI. |
| README examples | All five unchanged snippets pass syntax checks. Bash and both state examples execute unchanged. The two provider examples substitute the local byte client at provider construction and verify text and response cleanup. All use the extracted package. |
| Full conformance | `python3 conformance/run.py php` passes syntax, packaging, 73 groups, and the CLI self-test. Both mandatory native cURL/TCP groups fail at `stream_socket_server` with `Operation not permitted`. The runner keeps executable tools, their loop, Agent CLI integration, and HTTP incomplete. |
| Repository check | Root `npm run check` stops at the same three unrelated nested Biome configurations. |

ZIP SHA-256: `2881f211404f4a3daf88792528a49481e4968804de4177b1bcfa65b0ed8610df`.
The runtime package adds no file, class, or dependency; `Cooperative.php` is its
only changed runtime file. The coding CLI is still excluded from the package.

Composer is absent on PATH, and the usual Composer cache directories are absent.
A fresh request to `repo.packagist.org` fails DNS resolution. The native
[Opis JSON Schema 2.x documentation](https://opis.io/json-schema/2.x/) lists Draft
2020-12 support, and its [release list](https://github.com/opis/json-schema/releases)
identifies 2.6.0. It remains an untested candidate, with no adoption or dependency
download. Schema-validated tools, their shared Agent loop, HTTP, Composer,
Linux/native cURL/TCP, and final acceptance remain open. No paid request,
publication, commit, or push occurred.

## 2026-09-08 PHP core interruption checkpoint

`Agent.run` now preserves exceptions thrown into SDK-owned suspension points
through provider wrapping. It uses the existing `Cooperative` lookup, as the
CLI already does. The new regression failed before the one-line core change
because callers received `ProviderException` instead of their original exception.
Six cases cover both native providers, interruption during request and response
cleanup, and an extra custom provider wrapper. They verify original exception
identity, cleanup before the failure event, ordered events, unchanged prior
history and parent cancellation, and successful reuse of the agent.

`php check.php --offline` passes syntax, packaging, all 74 groups, and the CLI
self-test. All 74 copied groups, native subprocesses, the copied CLI self-test,
and all five README examples pass against the extracted ZIP. Both provider
examples substitute a local byte client; the remaining examples run unchanged.
All 27 archive entries match source. Preload checks verify hashes for the 24
runtime files and loaded paths for all 47 SDK classes/interfaces before and
after execution. ZIP SHA-256:
`089da90771f55937f556382c1e3c1024cb1c3b2c64187f2cb6669c7016caca55`.
Root `npm run check` still stops at the same three unrelated nested Biome roots.
TCP checks were not repeated for this error-propagation change; their recorded
listener restriction remains unresolved.

The GitHub connector is disconnected. Browser access reached the official Opis
2.6.0 repository, but automatic approval review rejected its source ZIP download
as declined. Fresh authorization was requested and remains unanswered, separately
from the existing C# validator request. No dependency was added or executed.
Schema validation, executable tools and their shared Agent loop, HTTP, and the
remaining platform/package acceptance are still open. Changes are uncommitted.

## 2026-09-08 Go core lifecycle checkpoint

Five new regressions failed against the previous core. Cancellation during the
completed-event callback returned success; a start callback could change the
history before it was copied; retained provider callbacks emitted during tools
or after the terminal event; event and handler mutations changed observations
and results; and provider-owned replay/output aliases remained live through
final delivery.

The core now snapshots prior history before `agent_start`, closes a child
context after each provider request, and copies mutable values before handing
them to callbacks or handlers. Tools retain the run's context after a request
closes. Cancellation during final delivery returns no result and does not emit
another terminal event. Small shared message/tool-call copy functions reuse the
existing snapshot logic. There is still one native agent loop and no new
dependency or runtime module.

Validation used the existing Go 1.26.4 binary in the local toolchain cache.
The default Go 1.24.4 launcher could not finish its automatic toolchain checksum
lookup because DNS access to the module proxy failed. Direct execution of the
already-present 1.26.4 toolchain required no download or checksum-setting change.

| Check | Result |
| --- | --- |
| Regression reproduction | All five new lifecycle groups fail before the core changes and pass afterward. Direct `Tool.Execute` also preserves its caller's argument buffer. |
| Source race suite | `go test -race -timeout 90s -skip 'TestProvidersLocalHTTP\|TestHTTPServerTCP\|TestHTTPBackpressureTCP' ./...` passes SDK, CLI, native RPC subprocess, provider, Bash, schema, and HTTP handler checks. |
| Formatting and analysis | Changed Go files pass `gofmt`; `go vet ./...` passes. |
| Copied module | The full offline race suite and CLI self-test pass with `GOWORK=off` and the module proxy disabled. Runtime function locations verify the copied core, tool, both provider, RPC, and HTTP server sources. Copied files match source before and after execution. Examples compile without sending provider requests. |
| TCP | The three mandatory TCP groups were run separately. Server, backpressure, and both provider cases fail at local socket binding with `operation not permitted`. |
| Repository check | Root `npm run check` stops at the same three unrelated nested Biome root configurations. |

TCP and final acceptance remain open. The existing C# and PHP validator download
authorization requests remain unanswered. No dependency was added, paid request
sent, or change committed.

## 2026-09-08 Go interactive console checkpoint

Native stdout and stderr regressions reproduced an interactive CLI hang after
cancellation. The console now owns nonblocking, close-on-exec duplicates of
borrowed POSIX files. Cancelling closes those duplicates to release pending Go
I/O. After the run and cancellation callback finish, cleanup restores the saved
blocking modes and retains the caller's original streams. Modes are captured
before any changes so stdout and stderr can share a file description. Descriptor
access uses `SyscallConn`, avoiding `File.Fd`'s change to blocking mode.

The console helper is 115 lines and uses the standard library. It adds no SDK
API or dependency. Custom input close callbacks are joined; custom writers
still need to return or cooperate with cancellation. The Windows path retains
that caller-supplied I/O contract. Guarded Bash requires POSIX.

| Check | Result |
| --- | --- |
| Reproduction | Both native-output cancellation cases fail before the console change and pass afterward. The failing fixtures explicitly release their pipes before returning. |
| Native ownership | Four behavior groups cover stalled stdout/stderr, initially blocking and nonblocking shared streams, close-on-exec flags, regular-file redirection, and conversation rollback. Original streams remain open and owned duplicates close. |
| Native subprocesses | Seven scenarios pass: idle input, requests, stdout, stderr, broken stdout, shared stdout/stderr, and Bash. Cleanup records show no active provider, original streams still open, and restored modes. The recorded Bash child no longer exists. |
| Actual entrypoints | Both `cmd/tinyagent` and `tiny_agent.go` compile and pass blacklist, native EOF, and idle SIGINT checks without provider credentials. The compiled command also reaches its prompt and exits with status zero after Ctrl-C in a PTY. Terminal flow-control backpressure was not tested. |
| Source and copied module | Full offline race checks and `go vet ./...` pass with the existing Go 1.26.4 toolchain. The copied module passes with workspace resolution and module downloads disabled. Console source-location checks run before the copied suite and every native console fixture process; copied module hashes match source through execution. |
| Platform compilation | CLI test binaries cross-compile for Linux arm64 and Windows amd64 with CGO disabled. Those binaries were not executed; native checks ran on macOS. |
| Repository check | Root `npm run check` still stops at the same three unrelated nested Biome root configurations. Changed Go files pass `gofmt`. |

A separate actual-binary probe confirms unfinished RPC backpressure. A valid
`get_state` command with a 131072-character ID begins its response, then blocks
when the parent stops reading. Closing stdin does not end the process within
one second; SIGINT also fails to end it within a further second. The owned
fixture was killed and reaped after the observation. The synchronous RPC writer
still needs cancellation and continued input handling under backpressure.
Existing passing RPC tests do not cover this case, and the matrix now records
the gap explicitly. TCP and final acceptance, plus the unanswered C# and PHP
validator download requests, remain open. Changes are uncommitted.

## 2026-09-08 Go RPC output checkpoint

The earlier RPC backpressure gap is fixed. A new native-pipe regression first
reproduced an ordinary response hanging after EOF. RPC now has one output worker
and a byte-bounded queue. Ordinary replies leave command input free to read EOF
and abort. A prompt reserves the session before its acknowledgement is queued,
then waits for acknowledgement delivery before entering the shared agent loop.
Events wait for delivery too, keeping successful history behind the final write.
The queue includes the active frame and line endings, and reserves space for one
bounded `output_queue_full` response.

RPC and the interactive console share `internal/nativeio`. The existing POSIX
descriptor helper moved there; it owns pollable, close-on-exec duplicates and
restores borrowed file modes after cleanup. RPC closes its owned input copy on
cancellation, permits output to drain, and closes its output copy if a pending
write remains stalled for one second. Short positive writes resume within the
same frame. Output remains available through slow cooperative provider/tool
cleanup. Generic input must unblock when closed; generic output must return or
unblock when closed. Shutdown joins the agent, writer, and input-close callback.
No public API or dependency was added.

| Check | Result |
| --- | --- |
| Reproduction | The native ordinary-reply EOF regression fails before the change and passes afterward. |
| Native pipes | Ten cases pass for stalled replies, acknowledgements, and agent events with EOF, context cancellation, abort, or reset. Provider work never starts behind a cancelled stalled acknowledgement; started providers finish cleanup before RPC returns. Caller-owned files remain open. |
| Actual command subprocesses | Eight scenarios compile and run `cmd/tinyagent`: EOF/SIGINT during stalled replies and acknowledgements, abort/reset behind an acknowledgement, output queue overflow, and a broken reader. Each exits with its transport error rather than hanging. |
| Delivery and limits | Short UTF-8 writes preserve framing; zero progress and invalid counts fail. Queue overflow includes the writing frame and emits exactly one bounded error when drained. Oversized output becomes a bounded error while later output remains usable. |
| Session lifecycle | A pending acknowledgement retains busy state. Partial output drains without interleaved frames. History commits only after successful final delivery, and remains unchanged if cancellation or output failure wins. Cooperative tool cleanup lasting longer than the write-drain timeout retains paired tool/end events. |
| Source and copied module | Full offline race suites and `go vet ./...` pass using the cached Go 1.26.4 toolchain. The copied suite runs with `GOWORK=off`, `GOTOOLCHAIN=local`, and module downloads disabled; module metadata confirms its isolated path. |
| Both entrypoints | `cmd/tinyagent` and `tiny_agent.go` compile from the copied module, pass their blacklist self-tests, and pass idle plus stalled-output EOF/SIGINT checks without provider credentials. |
| Platform compilation | SDK and CLI test binaries cross-compile for Linux arm64 and Windows amd64 with CGO disabled. Those binaries were not executed. |
| TCP | All three mandatory TCP groups were retried separately; server, backpressure, and both provider cases still fail at local binding with `operation not permitted`. |
| Formatting and repository check | Changed Go files pass `gofmt`. Root `npm run check` still stops at the same three unrelated nested Biome root configurations. |

This supersedes the RPC gap recorded in the preceding console checkpoint.
Native execution is verified on macOS. TCP, other-platform execution, and final
acceptance remain open, as do the unanswered C# and PHP validator download
requests. Changes remain uncommitted.

## 2026-09-08 Go HTTP transport checkpoint

Native `net/http` connection tests reproduced four gaps in the existing handler:
duplicate Authorization headers accepted the first credential; duplicate
Content-Type headers accepted the first media type; slow tool cleanup lost its
end events after the cancellation deadline expired; and failed final HTTP chunk
framing left successful history committed even though the client received
`unexpected EOF`.

Requests now require a single Authorization value when authentication is enabled
and a single Content-Type value for JSON bodies. Each pending event write owns
its cancellation deadline and joins its deadline callback before resetting it.
Healthy output stays available throughout cooperative provider/tool cleanup.

Prompt responses now close their HTTP connection after the last JSONL event.
This uses the standard library's unchunked streaming mode, eliminating the final
chunk that Go writes after returning from the handler. Event writes and flushes
remain synchronous, so successful history commits only after the final event's
write succeeds. Each prompt uses one connection; other routes retain connection
reuse. Clients still require `agent_end` to distinguish completion from an early
disconnect. There is no new public API, dependency, or custom HTTP framer.

`Serve` keeps address validation and TCP listening. A private helper owns the
existing native server lifecycle, letting tests supply bounded `net.Pipe`
connections through a small listener. Tests share the real server setup,
request parser, response writer, deadlines, and disconnect cancellation. TCP
tests and bounded-connection tests share request/event helpers.

| Check | Result |
| --- | --- |
| Reproduction | The duplicate-header, final-chunk failure, and slow-cleanup regressions fail before their fixes and pass afterward. The failed chunk case previously committed two history messages before the client saw a framing error. |
| Native HTTP transport | Nine groups cover all five routes; fixed-length and one-byte chunked UTF-8 bodies; exact and excessive body limits; malformed JSON; headers; busy and independent sessions; cancellation/deletion; event limits; and final delivery. |
| History and cleanup | A held final event retains prior history until delivered. Cancellation and failed delivery preserve a completed earlier transcript. Stalled native writes release on cancellation, deletion, disconnect, and shutdown. Silent providers receive disconnect cancellation. Deletion and shutdown remain pending until provider cleanup finishes. Slow tools retain paired tool/end events. |
| Source verification | The complete offline race suite passes with only the three separately tracked TCP groups excluded. `go vet ./...` passes. |
| Isolated module | The copied module passes the same race suite and vet with workspace resolution and downloads disabled. Module metadata confirms the copied path; runtime and test files match source through execution. Both CLI entrypoints compile, pass blacklist self-tests, and answer RPC state before clean EOF. Examples compile in the suite. |
| Platform compilation | Copied SDK test binaries cross-compile for Linux arm64 and Windows amd64 with CGO disabled. They were not executed. Native execution ran on macOS. |
| TCP | The three mandatory groups were retried. Server, backpressure, and both provider cases still fail at local TCP binding with `operation not permitted`. Bounded connection tests do not close this acceptance gap. |
| Repository check | Changed Go files pass `gofmt`. Root `npm run check` still fails at the same three unrelated nested Biome root configurations. |

TCP, other-platform execution, and final acceptance remain open. Existing C#
and PHP validator download requests remain unanswered. Changes are uncommitted.

## 2026-09-08 Rust RPC output checkpoint

Three regressions reproduced blocked output hiding EOF, hiding abort behind a
prompt acknowledgement, and delaying provider cancellation behind a state reply.
RPC now uses one output worker with a byte-bounded queue. Ordinary replies leave
input available; prompt acknowledgements and events await delivery receipts.
The queue includes the pending write and every LF, and reserves space for one
bounded overflow error. Overflow cancels the session and drains that error when
the reader permits it. Failed writes stop the driver.

Each pending delivery gets one second to drain after cancellation. This deadline
does not start a timer on cooperative tool cleanup. Normal shutdown waits for
the run and writer; dropping the driver cancels the run and aborts the writer.
The public API and dependencies are unchanged. Command handling remains in
`rpc.rs`; `rpc/output.rs` owns the queue and delivery lifetime.

History commits only after the final event's flush succeeds. A received final
line can precede that flush completing, so a concurrent state request may still
report `running`. The partial-input regression now checks both consistent states
and waits for the eventual committed history. Held-flush tests independently
prove that history cannot commit early and that cancellation or flush failure
preserves prior state.

| Check | Result |
| --- | --- |
| Reproduction | All three new EOF/abort regression groups fail before the change and pass afterward. A cancelled, blocked acknowledgement never starts the provider. |
| Delivery and limits | Three groups cover a pending UTF-8 frame in the 4 MiB queue budget, exactly one overflow error, final flush success/failure/cancellation, history commit, and release of a dropped writer. |
| Native command | The compiled CLI exits with an output error on EOF and SIGINT while its stdout pipe remains full. Both cases execute on macOS without credentials. |
| Cooperative cleanup | A tool remains active for more than the write timeout after abort; state stays readable, cleanup completes, and paired tool/end events drain. Existing Bash EOF/SIGINT and RPC framing/lifecycle groups also pass. |
| Full conformance | `CARGO_NET_OFFLINE=true CARGO_INCREMENTAL=0 python3 conformance/run.py rust` runs fifty passing offline tests. All five mandatory TCP tests still fail at denied local binding, so the runner correctly exits 1. |
| Static checks and examples | `cargo clippy --locked --all-targets -- -D warnings`, formatting, the offline tool-loop example, and CLI self-test pass. |
| Packaging | `cargo package --offline --allow-dirty` verifies a 36-file crate. Packaged runtime, test, example, lockfile, and README contents match source. The extracted crate's example and self-test pass; its CLI answers state through EOF and exits from stalled stdout on EOF/SIGINT. Cargo still warns about absent license/publication metadata. |
| Repository check | Root `npm run check` still stops at the same three unrelated nested Biome root configurations. |

The interactive console still awaits output without selecting cancellation;
its backpressure acceptance remains open. Source-fixture packaging, native TCP,
other-platform execution, and final acceptance remain open too. The native CLI
pipe checks do not prove cancellation of arbitrary caller-supplied blocking I/O.

Java still has no cached native schema validator. NetworkNT 3.0.7 and its official
dependency metadata were inspected, but automatic approval review rejected the
JAR download as user-declined. A fresh request for that validator and its declared
dependencies is pending alongside the earlier C# and PHP requests. No Java
dependency was installed or added to a manifest. Changes remain uncommitted and
the finish contract stays active.

## 2026-09-08 Rust interactive cancellation checkpoint

This resolves the interactive output gap recorded in the preceding checkpoint.
The shared console writer now applies cancellation to locking, writing, and
flushing. Each pending write gets one second to drain after cancellation. This
covers the banner, prompt, streamed text, final text, tool activity, provider
errors, self-test output, and HTTP startup diagnostics. Cooperative agent/tool
cleanup remains outside the output deadline. No new public API, dependency,
worker, or output queue was added to the interactive adapter.

The executable's final error report now uses bounded asynchronous stderr I/O.
It cannot hang on a full pipe after the adapter has returned an output error.
The process still shuts down Tokio's blocking stdio workers without waiting for
input or an unavailable output reader. Embedded callers remain responsible for
supplying async writers whose pending work can be released on drop.

| Check | Result |
| --- | --- |
| Reproduction | Blocked banner/self-test regressions and native stdout/stderr subprocess checks fail before the change and pass afterward. |
| Interactive output | Seven cases cover every interactive write site. Cancellation releases both output owners, drops the provider stream, and prevents a tool from starting while its start diagnostic is blocked. |
| Write and flush behavior | Self-test output settles under cancellation during either write or flush. A one-byte stream drains successfully after cancellation without losing or duplicating bytes. Existing conversation, tool replay, and failed-history rollback checks still pass. |
| Native pipes | Both stdout and stderr checks pass on macOS. The fixture receives the initial prompt, fills the selected pipe through one-byte `EAGAIN`, restores blocking mode, triggers another write, then sends SIGINT. This replaces an insufficient readiness-poll heuristic caught during packaged verification. |
| Full conformance | The runner records fifty-five passing offline tests and five mandatory TCP failures at denied local binding. The corrected native fixture is also rerun directly from source. The runner continues to exit 1. |
| Static checks and examples | Clippy with warnings denied, formatting, the offline tool-loop example, and CLI self-test pass. |
| Packaging | A 38-file crate verifies successfully. Thirty-five runtime/test/example/README/lockfile files match source. All nine selected console/RPC transport and cleanup groups pass from the extracted crate, including its compiled CLI with stalled stdout and stderr. The packaged example and self-test pass. |
| Repository check | Root `npm run check` still stops at the same three unrelated nested Biome root configurations. |

Low disk space was handled with package-scoped `cargo clean --package tiny-agent`,
removing only generated TinyAgent artifacts before rebuilding. No unrelated
source or cache was removed. Temporary verification files were removed.

Native TCP, other-platform execution, the package's shared-fixture dependency,
and the final requirement audit remain open. Cargo still warns about absent
publication/license metadata. Java, C#, and PHP dependency requests remain
unanswered. Changes are uncommitted; the finish contract remains active.

## 2026-09-08 Rust independent-package checkpoint

An extracted crate could not compile its full test suite: four SSE transcripts
and two JSON fixtures were referenced outside the package. Rust now bundles
those six files under its own `conformance/` directory. Tests read the bundled
files, while the workspace runner checks them byte for byte against the shared
sources before invoking Cargo. Missing, changed, or extra copies fail that gate.

The crate also includes the protocol reference, with the same freshness check.
Its README links to that local copy and documents how to refresh shared assets.
The protocol's workspace-only completion reference is plain text so the bundled
document does not link to a missing finish-contract file. No runtime behavior,
public API, dependencies, or agent loops changed.

| Check | Result |
| --- | --- |
| Reproduction | `cargo test --locked --no-run` in the old extracted package fails at all six external fixture paths. |
| Shared source checks | The unmodified conformance command reaches Cargo after the new asset check. Fifty-five offline tests pass; the five mandatory TCP tests still fail at denied binding, and the runner exits 1. Clippy, the example, and self-test pass. |
| Asset freshness | A separate runner probe rejects changed, missing, extra, and obsolete fixture copies and a changed protocol before dispatching Cargo. Restoring the exact copies restores the four expected command dispatches. The probe stubs Cargo only to test the gate; native SDK evidence comes from the actual source and extracted-crate runs. |
| Package verification | `cargo package --offline --allow-dirty` creates and verifies a 45-file crate. Forty-two source/test/example/document/fixture/lockfile files and the original manifest match before and after execution. |
| Independent extraction | Cargo metadata identifies the extracted directory as its sole workspace and locates every target there. All fifty-five offline tests pass from that directory, including provider/tool fixtures, actual RPC subprocesses, Bash, native console pipes, and HTTP lifecycle checks. The separate TCP tests remain excluded from this offline command. |
| Packaged tools and examples | Clippy passes with warnings denied across all packaged targets. The offline SDK example prints `hello`, and the packaged CLI self-test passes. |
| Formatting and repository check | Rust formatting and Black pass. Root `npm run check` still stops at the same three unrelated nested Biome configurations. |

This closes the shared-fixture packaging gap. Native TCP, other-platform
execution, and the final requirement audit remain open. Cargo still reports
unassigned license/publication metadata; nothing was published. Java, C#, and
PHP dependency authorizations remain pending. Temporary probe and extraction
files were removed. Changes are uncommitted and the finish contract stays active.
