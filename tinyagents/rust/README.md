# Rust SDK

Status: importable core, schema validation, streaming OpenAI and Anthropic
providers, cancellable Bash, CLI, RPC, and HTTP implemented. All sixty source tests
pass, including provider and server TCP checks. Package and final acceptance
remain tracked in the workspace's `FINISH_CONTRACT.md`.

The crate imports as `tiny_agent`. The `tiny_agent.rs` entrypoint handles
signals and terminal I/O; `cli::run` connects the CLI to the async SDK.

Run from this directory:

```sh
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
cargo run --locked --example offline
cargo run --locked -- --self-test
cargo run --locked -- --mode rpc
cargo run --locked -- serve --port 8080
```

The example completes a native tool loop without credentials or network access.
The crate includes its test fixtures and protocol reference, so the same tests
and examples run from an extracted `cargo package` archive. Local package
verification and the packaged example and CLI self-test pass.
Checks have run with Rust 1.96.1. Direct dependencies use exact versions, and the
lockfile records transitive versions. The schema validator has external loading
disabled even when a consuming application enables its HTTP or file features.

The workspace's `conformance/` directory and `PROTOCOL.md` are authoritative.
Its runner verifies the bundled copies byte for byte before running Rust checks.
After changing those shared files, refresh the copies from this directory:

```sh
cp ../../conformance/*.json conformance/
cp ../../conformance/providers/*.sse conformance/providers/
cp ../../PROTOCOL.md PROTOCOL.md
python3 ../../conformance/run.py rust
```

Use `OpenAIProvider::new()?` for the Responses API or
`AnthropicProvider::new()?` for Messages. Construction requires no credentials.
Requests use `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` unless `api_key` is set on the
provider. Each provider exposes its `client` and `endpoint` for embedding and
tests. The default client has a 180-second timeout; Anthropic defaults to 8192
output tokens.

Both providers buffer tool arguments until complete and preserve opaque replay
metadata. OpenAI retains output items, including reasoning data. Anthropic
retains the complete assistant message, including thinking signatures and usage.
Incomplete responses fail the run. The provider stream ends at its terminal
event without waiting for HTTP EOF.

`Tool::new` takes a definition and an async handler. It validates the schema at
construction and arguments before calling the handler. Decode validated
`serde_json::Value` arguments into the handler's concrete type. Handlers receive
a run-scoped `CancellationToken` and must honor it during active work.

Implement `Provider::stream` with an async method returning `ProviderStream`.
The stream yields `TextDelta` values followed by one complete `Turn`. The core
drops the request future or stream to cancel provider I/O. A provider must own
its resources so dropping it releases them.

`Agent::new` takes a model, instructions, provider, and tools. `Agent::run`
returns final text and history. `run_with_events` accepts an async event callback
and uses the same loop. The callback applies backpressure; returning an error
stops the run and prevents further event delivery. Callers replace their saved
history only after a successful return.

`bash_tool()` creates the CLI's native tool. `run_bash` bounds combined output
and cancels Unix process groups even after the initial shell exits. Other
platforms stop the direct child. The blacklist is a guardrail, not a sandbox.

`run_rpc(Arc<Agent<P>>, input, output, cancellation)` runs one session over
Tokio streams. Consume its output concurrently. Each prompt acknowledgement
precedes events; successful runs commit history, while failed or cancelled
runs retain the prior history. EOF and cancellation await cooperative cleanup.
Ordinary replies queue without blocking command input. The queue holds at most
4 MiB, including the pending write and line endings, with room reserved for one
overflow error. Acknowledgements and events wait for delivery; history commits
after the final event's flush. A state query racing that flush can still report
the run as active.

Dropping the driver future cancels the active run and aborts its output worker.
A pending output delivery gets one second to drain after cancellation; an output
error ends the driver. This deadline does not shorten cooperative tool cleanup.
Normal shutdown joins the run and writer. Custom async writers must release
their resources when dropped.

The CLI defaults to OpenAI. Set `TINYAGENT_PROVIDER=anthropic` and
`ANTHROPIC_API_KEY` to use Anthropic, and `TINYAGENT_MODEL` to override the model.
Interactive mode streams text to stdout and sends tool activity to stderr.
RPC stdout contains only protocol objects. Neither `--self-test` nor RPC state
commands require credentials.

Console writes and flushes get one second to finish after cancellation. This
also covers self-test output and the HTTP startup message. The executable gives
its final error diagnostic at most one second, then exits with failure if output
could not complete. Embedded callers must supply async writers whose pending
work can be released on drop.

`serve(Arc<Agent<P>>, HttpOptions)` starts the in-memory HTTP server. Its options
are `address: SocketAddr` (default `127.0.0.1:8080`), `token: String`, and
`allow_remote: bool`. Port zero chooses an available port. Non-loopback
addresses require a token and explicit remote opt-in. Every route checks a
configured token, and the server adds no CORS headers.

The returned `HttpServer` exposes `url()`, `wait().await`, and
`close().await`. Closing stops the listener, cancels active runs, and waits for
cooperative cleanup. Dropping the handle requests shutdown without waiting.
The CLI accepts `serve --host IP --port PORT --token TOKEN --allow-remote`;
`TINYAGENT_TOKEN` supplies its default token.

The five routes and payloads follow [PROTOCOL.md](PROTOCOL.md). HTTP and
RPC share session history and cancellation. Hyper handles HTTP/1 framing, with
one request per connection. A bounded event channel applies backpressure.
The terminal callback waits for the connection to finish writing before history
can commit. Cancellation gives a stalled connection one second to drain.
Deletion releases session ownership before awaiting cleanup, so a disconnected
deletion request cannot leave a closed session in the map.

The core uses owned history values and does not expose mutable agent settings.
`types.rs` defines events, history, and the provider interface. `agent.rs` owns
the loop; `tool.rs` owns schema validation and handlers. `providers/` contains
request serialization, API-specific parsers, and a shared bounded SSE reader.
`bash.rs` owns subprocess execution; `session.rs` owns history and active-run
lifecycle; `rpc.rs` owns JSONL commands; `rpc/output.rs` owns the bounded writer
and delivery receipts; `lines.rs` preserves partial input across cancelled reads.
`http/` separates routes and session ownership, HTTP body streaming, and
listener/connection lifetime. The tests consume the shared argument, SSE, and RPC
fixtures. Sixty source tests pass, including
real RPC subprocesses, Bash cancellation, and HTTP/1 over bounded in-memory
streams. RPC tests also cover blocked replies and acknowledgements, queue
overflow, held or failed final flushes, and slow cooperative cleanup. The compiled
CLI exits on EOF and SIGINT with full RPC stdout, and on SIGINT with full
interactive stdout or stderr, on macOS. Five mandatory TCP tests also pass:
two provider checks and three SDK/CLI server checks. The workspace's
`FINISH_CONTRACT.md` records current evidence and remaining acceptance work.
