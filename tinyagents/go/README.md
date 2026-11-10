# Go SDK

Status: experimental reference implementation. It has the native core, both
streaming providers, a coding CLI, RPC, and HTTP. Sessions are held in memory:
durable sessions, resume, recovery and compaction are not implemented, so the
"Durable sessions and compaction" section of the protocol does not apply here.
This package is not in CI. Its checks were last run in September 2026 on macOS
arm64 and have not been re-run since.

Use Go 1.26.4 or newer. The module imports as `tinyagent` within this directory.
An application in a separate module can use a local `replace` directive while
the SDK remains unpublished.

```sh
cd tinyagents/go
go test -race ./...
go vet ./...
go run ./cmd/tinyagent --self-test
go run ./examples/echo
```

The example needs `OPENAI_API_KEY`. Tests use synthetic provider responses and
require no credentials. The complete test command includes local TCP fixtures;
restricted environments report their inability to bind as test failures.

The module includes its conformance fixtures and protocol reference, so its full
suite runs outside this repository. The shared runner checks these copies byte
for byte before dispatching Go. After changing shared assets,
refresh them from this directory:

```sh
cp ../../conformance/*.json conformance/
cp ../../conformance/providers/*.sse conformance/providers/
cp ../../PROTOCOL.md PROTOCOL.md
python3 ../../conformance/run.py go
```

`NewTool` checks a JSON Schema and keeps a native handler. Arguments arrive as
`json.RawMessage` after validation; decode them into the handler's concrete
argument type. Ordinary handler errors and oversized outputs become bounded
tool observations. Handlers receive the run's `context.Context`.
Each handler receives its own argument buffer.

`NewAgent` takes a model, instructions, provider, and tools. `Run` accepts prior
history and an optional `RunOptions.OnEvent` callback. It returns final text,
history, and provider output. Returning an error from the callback cancels the
run. Callers only replace their history after a successful return.

The core snapshots prior history before the start callback. Event payloads and
provider results are detached from the transcript before callbacks or handlers
can change them. Each provider request has a child context that closes before
tools execute or the final event is delivered. A retained text callback rejects
delivery after that request ends; the tool's run context remains active.
Cancellation during final-event delivery returns no result and does not emit a
second terminal event. Provider callbacks are synchronous and must finish before
`Stream` returns.

Use `OpenAIProvider` or `AnthropicProvider`. Both support an injected `http.Client`
and endpoint for tests, stop reading at terminal provider events, and preserve
opaque replay data in assistant history. Anthropic defaults to 8192 output tokens.
Its `max_tokens` stop reason fails the run because the answer is incomplete.

`RunRPC(ctx, agent, input, output)` serves one session over JSONL. Command replies
queue so input can keep reading abort and EOF. The 4 MiB queue includes the
current write and all line endings; overflow cancels the connection. A prompt
reserves the session immediately, but its acknowledgement must finish writing
before agent work starts. Events also wait for delivery. Only successful,
uncancelled runs whose final event was delivered replace session history.

EOF and interrupt cancel the active run and close the connection. Reset and abort
cancel the run while allowing the connection to continue if output drains.
Pending writes get one second to drain after cancellation; healthy output stays
available during cooperative tool cleanup. Partial writes resume within their
frame, so another response cannot interrupt an unfinished JSONL line.

On POSIX, RPC borrows native files through owned nonblocking duplicates, closes
those duplicates to interrupt stalled I/O, and restores original blocking modes
before returning. Keep the supplied files open and unused elsewhere during RPC.
Other inputs must release pending reads on `Close`. Other writers must return
from `Write` or release it on `Close`; a fatal output error may close them. RPC
joins its writer, input-close callback, and agent work before returning. Callers
must consume output concurrently. The native pipe and subprocess checks have
only been run on macOS; the Windows path retains cooperative caller-supplied I/O.

`Serve(agent, HTTPOptions{Address: "127.0.0.1:8080"})` starts the five baseline
routes in [PROTOCOL.md](PROTOCOL.md). `server.URL()` reports its address; port zero
selects an available port. Call `server.Close()` when finished. `server.Wait()`
waits for the listener and active runs to stop.

Configure `Token` for bearer authentication. A non-loopback address requires
both `AllowRemote: true` and a token. Repeated Authorization headers are rejected.
JSON request bodies require one Content-Type header. No CORS headers are added.

Prompt responses stream JSONL and close their HTTP connection when finished.
Each prompt therefore uses a separate connection. Every event is flushed before
the core continues; no final chunk remains to be written after history commits.
Clients must require the terminal `agent_end` event: EOF alone can also mean a
disconnect. Other routes retain normal HTTP connection reuse.

Cancellation gives a pending write one second to finish before interrupting it.
Healthy output remains available while a tool or provider finishes cooperative
cleanup, even when cleanup takes longer than that write deadline. Custom
providers and tools must honor their run context; deletion and shutdown wait
for them to finish.

Native `net/http` client/server checks run over bounded `net.Conn` pairs. They
cover all routes, request framing and limits, duplicate headers, session
concurrency, final delivery, disconnects, cancellation under backpressure, and
joined cleanup. These checks supplement the mandatory TCP tests.

The implementation is split by responsibility:

- `agent.go` owns the run and tool loop; `types.go` defines its values.
- `tool.go` validates arguments and calls handlers. `schema.go` loads bundled
  standard schema definitions so tool construction requires no network access.
- `openai.go` and `anthropic.go` translate provider requests and events.
- `sse.go` reads bounded UTF-8 provider streams and owns HTTP response cleanup.
- `session.go` owns history commit and cancellation for RPC and HTTP.
- `rpc.go` handles JSONL commands and input framing; `rpc_output.go` owns bounded
  output and delivery receipts.
- `http.go` handles routes and request limits; `server.go` owns the listener.
- `bash.go` provides the optional guarded Bash tool and bounded process output.
- `cli/` connects terminal I/O to the same core.
- `internal/nativeio/` shares borrowed POSIX file handling between RPC and the
  interactive console.

The standalone `tiny_agent.go` delegates to `cli.Run`. The `go.work` file at the
repository root keeps this entrypoint runnable from there:

```sh
go run tinyagents/go/tiny_agent.go
go run tinyagents/go/tiny_agent.go --mode rpc
go run tinyagents/go/tiny_agent.go serve --port 8080
```

The CLI reads `TINYAGENT_PROVIDER`, `TINYAGENT_MODEL`, and the selected provider's
API key. Server flags are `--host`, `--port`, `--token`, and `--allow-remote`;
`--token` defaults to `TINYAGENT_TOKEN`. Credentials are required when a provider
request starts, so RPC state commands work without them.

The interactive POSIX console borrows native input, output, and diagnostic files.
It uses owned nonblocking duplicates so cancellation can release idle input or
stalled output. Cleanup closes those duplicates and restores the original
blocking modes before returning; the caller's files stay open. Keep the supplied
files open and unused elsewhere while the console owns their I/O. Shared stdout
and stderr and regular-file redirection are supported. Native checks cover
Ctrl-C during requests, Bash, and stalled writes, plus broken-output cleanup.
They have only been run on macOS; Linux is unverified.

Custom input streams must support `Close` to release a pending read. Custom
writers must return or cooperate with cancellation. The console waits for
input-close cleanup before returning. The Windows path retains cooperative
caller-supplied I/O; guarded Bash requires POSIX.

To run the CLI from other working directories, build or install `./cmd/tinyagent`
from this module. Bash runs in the directory where the resulting command starts.

## Verification

Last verified in September 2026 on macOS arm64, and not covered by CI. At that
time `go test -race ./...` passed, including the tests that bind loopback
sockets, and a copy of the module passed the same suite with Go workspace resolution disabled.
Linux and Windows are unverified.
