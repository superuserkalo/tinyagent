# TinyAgent Elixir SDK

Native tool runs with ordered events, immutable completed history, and streaming
OpenAI Responses or Anthropic Messages. The core and providers support scoped
cancellation and cooperative cleanup. `Rpc` adds native JSONL transport and
committed session history. The SDK, coding CLI, RPC, and HTTP server share one
Agent loop with validated sequential tools and bounded, cancellable Bash.
`HttpServer` exposes authenticated HTTP sessions around the same session controller.
`ToolDefinition` is provider metadata only.

Status: experimental reference implementation. Sessions are held in memory:
durable sessions, resume, recovery and compaction are not implemented, so the
"Durable sessions and compaction" section of
[PROTOCOL.md](../../PROTOCOL.md) does not apply here. This package is not in
CI. Its checks were last run in September 2026 on macOS arm64 and have not been
re-run since.

## Use from a Mix application

Add a local dependency to your application's `mix.exs`:

```elixir
{:tiny_agent_sdk, path: "/path/to/tinyagent/tinyagents/elixir"}
```

The SDK declares Elixir 1.15 or later and pins Mint 1.10.0, Jason 1.4.5,
Bandit 1.12.5, Plug 1.20.3, and Thousand Island 1.5.0. The lockfile pins their
five transitive dependencies. Tool schemas use JSV 0.22.0 with abnf_parsec 2.1.0,
Texture 1.2.1, IDNA 7.1.0, and NimbleParsec 1.4.2; all versions and checksums
are locked. Mint 1.10.0 fixes unbounded response-line buffering
and chunk-size parsing. The SDK was last checked in September 2026 on macOS
with Elixir 1.20.4 and OTP 29. Linux, and Elixir 1.15, the declared minimum,
are unverified.

On macOS and Linux, building also requires a C11 compiler and the Erlang NIF
headers shipped with Erlang development packages. The build uses `cc`; `CC` may
name another compiler executable. The POSIX bridge owns native descriptors and
signal handling for RPC and the coding CLI. A separate native port executable
supervises Bash and captures its output. Other platforms retain the Elixir
core, providers, and parsed sessions without that bridge. Native I/O checks use
Python 3 to drive Elixir subprocesses; the SDK does not need Python to run.

## Run an agent

```elixir
alias TinyAgent.{Agent, AgentEvent, OpenAIProvider}

provider = OpenAIProvider.new()
agent = Agent.new(provider, "gpt-5.6", instructions: "Answer briefly.")

result = Agent.run(agent, "Hello", on_event: fn
  %AgentEvent{kind: {:text_delta, text}}, _token -> IO.write(text)
  _event, _token -> :ok
end)

IO.puts("\nCompleted: #{result.text}")
```

Set `OPENAI_API_KEY` before running the example. Import and construction need
no credentials. `Agent.run` accepts `history`, `on_event`, `cancellation`, and
`run_id` options. Pass `result.history` to continue a conversation. A `RunResult`
contains final `text`, completed `history`, and the final provider `output`.
Failed or cancelled runs do not return a result.

Each run owns its history, event sequence, and child cancellation token. The
configured agent can serve independent callers. Events contain `run_id`, a
`sequence` starting at one, and `kind`: `:start`, `{:text_delta, text}`,
`{:tool_start, call_id, name, arguments}`, `{:tool_end, call_id, name, output, is_error}`,
`{:completed, text}`, `{:failed, message}`, or `:aborted`. `AgentEvent.to_map`
produces the shared protocol object. Maps, lists, structs, and event values
remain immutable.

Callbacks run synchronously in the calling process. Provider cleanup precedes
final-event delivery, which must finish before `run` returns. Consumer errors,
throws, and exits retain their original values even when a provider wraps or
swallows them. Failed consumers receive no later event. Failed or cancelled
completed-event delivery cannot return a result or emit a second terminal event.

Pass a `CancellationToken` to `run`; another process can call
`CancellationToken.cancel(token)`. Callback tokens can cancel their run without
cancelling the caller's token. Provider deadlines remain `ProviderError`
failures; explicit cancellation raises `Cancelled`. Providers and callbacks
must return or cooperate with cancellation for cleanup to finish. The caller
owns the provider and must keep it alive until its runs finish.

The core creates no background process. A private process-dictionary entry
holds callback order and the first consumer failure, because provider callbacks
do not return updated state. Each run uses a unique key and removes it on exit;
nested runs preserve each other's state. Retained callbacks cannot emit after
provider completion. Custom providers must implement `stream/3`, invoke callbacks
in order in the calling process, and finish request cleanup before returning.
An unknown tool rejects its entire batch before any handler executes.

## Native tools

This example runs without provider credentials:

```elixir
alias TinyAgent.{Agent, CancellationToken, Tool}

defmodule EchoExampleProvider do
  alias TinyAgent.{ProviderTurn, ToolCall, ToolResultMessage}
  defstruct []

  def stream(_provider, request, _options) do
    case List.last(request.history) do
      %ToolResultMessage{output: output} -> ProviderTurn.new(output, [], [], [])
      _ -> ProviderTurn.new("", [ToolCall.new("call", "echo", %{"text" => "hello"})], [], [])
    end
  end
end

tool = Tool.new("echo", "Return text", %{
  "type" => "object", "properties" => %{"text" => %{"type" => "string", "minLength" => 1}},
  "required" => ["text"], "additionalProperties" => false
}, fn arguments, cancellation ->
  CancellationToken.check!(cancellation)
  arguments["text"]
end)

agent = Agent.new(struct!(EchoExampleProvider), "fixture", tools: [tool])
IO.puts(Agent.run(agent, "go").text)
```

`Tool.new/4` compiles a Draft 2020-12 schema and a native two-argument handler.
JSV owns schema and argument validation. Invalid schemas, unresolved or external
references, and reference cycles that cannot descend into data fail at construction.
Local references, anchors, dynamic references, and recursive data are supported.
Formats remain annotations; validation disables casting and preserves the
original immutable arguments without defaults or property removal.

Agent executes handlers sequentially and checks cancellation before entry.
Invalid arguments, ordinary exceptions, invalid UTF-8, and results above 131072
UTF-8 bytes become bounded error observations. Cancellation joins cooperative
handler cleanup, pairs started tools with end events while delivery remains usable,
and aborts the run. Consumer errors, throws, and exits propagate unchanged.
Handler throws and exits also propagate after the paired end. Every provider
request owns a callback scope that closes before tool execution or the next request.

## Own a session

```elixir
alias TinyAgent.{Agent, OpenAIProvider, RpcSession}

agent = Agent.new(OpenAIProvider.new(), "gpt-5.6")
rpc = RpcSession.new(agent, fn value, _token -> IO.puts(Jason.encode!(value)) end)

try do
  RpcSession.handle(rpc, %{"id" => "state", "type" => "get_state"})
after
  RpcSession.close(rpc)
end
```

This state query needs no credentials. `handle` accepts decoded JSON maps for
`prompt`, `abort`, `new_session`, and `get_state`; an optional `id` must be a
string. It reports invalid commands through the emitter. Direct `get_state`,
`cancel`, `stop`, `wait_idle`, and `close` calls are also available.

Each session owns a linked controller and at most one monitored run process.
The controller remains responsive while that run acknowledges a prompt, calls
the provider, or delivers events. Acknowledgement finishes before the core
starts. History commits only after successful terminal delivery and run exit.
Failure or cancellation preserves previous history. Separate sessions share
agent configuration while keeping independent history and cancellation.

The emitter receives a cancellation token for prompt acknowledgements and run
events. These callbacks run in the worker and must finish delivery before
returning. Ordinary command responses receive `nil` and run in the command
caller's process. A transport must serialize these concurrent writes and bound
queued output. Callback failures are never retried. This adapter does not yet
provide JSONL framing, byte limits, stdin/stdout, or disconnect detection itself.
Use `Rpc` below when you need the native transport.

Reset clears history immediately and waits for cooperative run cleanup before
allowing another prompt. `cancel` requests cancellation and returns the active
run ID or `nil`; `stop` also closes admission. `wait_idle` joins the active run.
`close` closes admission, cancels, and joins both worker and controller; repeated
close calls are harmless. From its own run callback, reset requests cleanup
without waiting, and `wait_idle` or `close` returns `false` to avoid waiting on
itself. External close returns `true` after cleanup. Keep the creating process
alive while using the session; its normal exit also cancels and joins the run.
Providers and emitters must cooperate with cancellation for cleanup to finish.

## Serve JSONL RPC

```elixir
alias TinyAgent.{Agent, OpenAIProvider, Rpc}

agent = Agent.new(OpenAIProvider.new(), "gpt-5.6")
Rpc.run_stdio(agent)
```

Pipe `{"id":"state","type":"get_state"}` followed by LF into this example
to query the empty session without credentials. Set `OPENAI_API_KEY` before
prompting the provider. This API and the coding CLI serve the configured Agent
and its registered tools.

`Rpc.run_stdio(agent, cancellation: token)` serves stdin/stdout and temporarily
handles SIGINT. `Rpc.run(agent, input: read_fd, output: write_fd)` serves distinct
POSIX file descriptors. It accepts `cancellation` and `handle_interrupt: false`;
the latter is the default for embedding. Only one connection may own SIGINT.
Its previous handler is restored on return or owner exit. EOF and SIGINT cancel
the run, await cooperative cleanup, and return `:ok`. External cancellation
raises `Cancelled` after cleanup. Descriptor failures raise `RpcError` with a
stable `code`; invalid options raise an argument or missing-key error.

The descriptors are borrowed exclusively for the call. Keep their original
handles open and avoid concurrent reads, writes, or status-flag changes. The
bridge duplicates them, uses nonblocking I/O, restores status flags, and closes
its own copies. Pipes, regular files, and character devices are supported on
macOS and Linux; sockets and non-POSIX platforms are not supported by this driver.
The library does not close the caller's descriptors or exit the VM.

The reader accepts commands while output is queued. A single writer preserves
JSONL frames and enforces the 1 MiB line limit excluding LF and the 4 MiB queue
limit including LF and the full current frame. Prompt acknowledgements and
events await complete delivery. History commits after final delivery and worker
exit. An oversized object becomes `output_too_large` and cancels its active run.
Queue overflow cancels the connection and reports `output_queue_full` after
finishing a partially written line if output drains.

Cancelled writes have one second per frame to drain. A stalled or broken output
ends the connection, releases waiting callbacks, and raises a transport error
after provider cleanup. Such a failure can leave the last line incomplete.
Successful draining allows the session to continue after abort. Driver return
joins its reader, writer, session, and active run. Owner exit also cancels those
processes and releases descriptors. Providers must cooperate with cancellation;
shutdown continues waiting while a provider holds its cleanup.

## HTTP sessions

```elixir
alias TinyAgent.{Agent, HttpServer, OpenAIProvider}

agent = Agent.new(OpenAIProvider.new(), "gpt-5.6")
{:ok, server} = HttpServer.start_link(agent, port: 8080, token: "example-token")
IO.puts(HttpServer.url(server))

try do
  IO.gets("Press Enter to stop.\n")
after
  HttpServer.close(server)
end
```

Health and session creation need no provider credentials. Prompts use the
configured provider. Options are `host`, `port`, `token`, and `allow_remote`.
The default address is `127.0.0.1:8080`; port zero chooses an available port.
Non-loopback binding requires explicit `allow_remote: true` and a nonempty bearer
token. Authentication protects all five routes, including `/health`. No CORS
headers are added. Bodies and JSONL events are bounded to 1 MiB.

Bandit parses and frames HTTP/1. Each connection serves one request. The session
worker waits for every native write, including final chunk framing, before
history can commit. A synchronous handoff holds at most one event. A separate
socket reader detects silent disconnects and cancels only that run. Cancelled
pending writes have one second to drain; cooperative provider cleanup has no
deadline while the response remains healthy. A client closing after successful
final framing does not undo completed history. If EOF arrives while the final
write is pending, the writer confirms its result before the run can commit.

Deletion retains its session until cleanup finishes. `stop` requests shutdown;
external `close` joins sessions, connections, and the listener. `close` returns
`false` in a provider callback to avoid waiting on itself. Keep the process that
called `start_link` alive; its exit also cancels and joins cleanup. The server
does not install signal handlers, close the provider, or exit the VM.

## Stream a provider turn

```elixir
alias TinyAgent.{OpenAIProvider, ProviderRequest, UserMessage}

provider = OpenAIProvider.new()
request = ProviderRequest.new("gpt-5.6", "Answer briefly.", [UserMessage.new("Hello")])

turn = OpenAIProvider.stream(provider, request,
  on_text: fn text, _cancellation -> IO.write(text) end)

IO.puts("\nCompleted: #{turn.text}")
```

Set `OPENAI_API_KEY` before calling `stream`. Construction and import require no
credentials. `AnthropicProvider.new()` and `AnthropicProvider.stream()` use
`ANTHROPIC_API_KEY`; select the corresponding model in `ProviderRequest`.
Both constructors accept `api_key`, `endpoint`, and a `timeout` in milliseconds.
Anthropic also accepts `max_tokens`, defaulting to 8192.

Pass `cancellation: TinyAgent.CancellationToken.new()` to `stream`. Another
process can call `CancellationToken.cancel(token)`. The text callback receives
a child token that includes the request deadline; cancelling it does not cancel
the parent. Callbacks run synchronously in the caller's process and must return
for cleanup to advance. Success returns a `ProviderTurn` containing `text`,
`tool_calls`, `assistant`, `replay`, and `output`. Append `turn.assistant` and
matching `ToolResultMessage` values to the next request's history to preserve
provider replay metadata. Elixir values are immutable; maps represent JSON
objects and lists represent arrays.

Requests use Mint HTTP/1 with a fresh connection. The default deadline is
180 seconds, including response parsing, callbacks, and cleanup. Reads check
cancellation at least every 50 milliseconds while stalled. Connection setup
and request writes use native timeouts capped at 30 seconds; cancellation does
not forcibly interrupt a DNS lookup or TLS handshake. The connection closes
before the provider returns or raises. Cleanup drains only messages belonging
to that socket. Providers stop reading at their terminal event and preserve
opaque reasoning and thinking-signature data for replay.
OpenAI requests include encrypted reasoning with `store: false`, so the next
request can replay that metadata without server-side conversation storage.

Malformed or incomplete provider responses raise `TinyAgent.ProviderError`.
Explicit cancellation raises `TinyAgent.Cancelled`. Deadline expiry raises
`ProviderError`, including when a callback fails after expiry. SSE lines,
events, and cumulative tool arguments each have a 1 MiB byte limit. Providers
do not validate tool arguments against their advertised schemas or execute tools.

## Bash and the coding CLI

```elixir
alias TinyAgent.Bash

IO.puts(Bash.run("printf 'hello\\n'; printf 'notice\\n' >&2"))
```

`Bash.run(command, cancellation: token)` starts a fresh, noninteractive command
with `/dev/null` as stdin. It returns an `exit_code`, `stdout`, and `stderr`
observation. Empty commands, NUL bytes, and invalid UTF-8 are rejected. The
shared blacklist returns exit code 126 for obvious destructive commands. It
is not an operating-system sandbox.

Standard output and error share `Bash.max_output_bytes()`, or 131072 UTF-8 bytes,
including the final observation envelope and replacement of invalid output
bytes. Overflow raises `ProviderError` after cleanup. `CancellationToken.cancel`
stops the command and raises `Cancelled` after cleanup. Calls own independent
ports and buffers and preserve unrelated caller messages and `trap_exit` settings.

The native helper captures both pipes with a byte limit before sending results
to Elixir. A Bash supervisor owns the command's process group. Normal completion
removes remaining background group members; cancellation signals the live
supervisor and waits for it to exit. Processes that deliberately escape the
group remain outside this cleanup. The helper also stops its command when the
owning [Erlang port](https://www.erlang.org/doc/system/ports.html) closes. A forced
owner exit triggers cleanup, but that owner cannot await it. Bash startup
environment variables are removed from the child environment. `Bash.run`
installs no VM signal handler, and commands have no execution deadline.

Run `./tiny_agent.exs` with `OPENAI_API_KEY` set. The entrypoint installs this
local SDK through Mix when it is not already loaded. Its CLI implementation
is in `cli.ex`, outside the imported SDK. A precompiled SDK can also be supplied
with Elixir's `-pa` option. The CLI streams OpenAI text and preserves neutral
history and provider replay. Invalid arguments, ordinary Bash failures, and
oversized output become bounded observations so the model can recover.

Each turn runs in an owned task while the console watches SIGINT. Input,
streamed text, and diagnostics use the SDK's nonblocking descriptor bridge.
Ctrl-C cancels an idle prompt, provider request, Bash command, or stalled output
and waits for cleanup. Previous descriptor flags and signal handling are restored
on return. Successful turns commit history; errors and cancellation preserve the
previous transcript. EOF accepts a final prompt without a newline.

The same entrypoint also supports `./tiny_agent.exs --mode rpc` and
`./tiny_agent.exs serve`. Both start without provider credentials. HTTP reads
`TINYAGENT_HOST` (default `127.0.0.1`), `TINYAGENT_PORT` (default `8080`),
`TINYAGENT_TOKEN`, and `TINYAGENT_ALLOW_REMOTE=1`. The CLI owns SIGINT while
serving and joins session and server cleanup before restoring its handler.

## Check and package

On a host that permits local listeners:

```sh
mix deps.get
mix compile --warnings-as-errors
mix format --check-formatted
mix test
mkdir -p dist
mix hex.build --output dist/tiny_agent_sdk-0.0.1.tar
elixir tiny_agent.exs --self-test
```

Hex creates a local package in `dist/` using the file list from `mix.exs`.
It includes twenty-five Elixir runtime source files, both POSIX C sources and their
build helper, project manifest, lock, README, and MIT license. Tests and the
CLI files are excluded. The bridge and Bash port executable compile into the
application's `priv` directory; prebuilt native binaries are excluded from the source package.
The package has not been published to a registry.

When dependencies are already compiled, the direct check avoids Mix's TCP-based
filesystem lock:

```sh
TINYAGENT_ELIXIR_DEPS=/path/to/compiled/lib elixir check.exs --offline
TINYAGENT_ELIXIR_DEPS=/path/to/compiled/lib elixir check.exs
```

The dependency directory must contain an `ebin` directory for every library in
`mix.lock`, with the locked versions. It defaults to `_build/dev/lib`. The helper checks
formatting, compiles the SDK and CLI with warnings treated as errors, runs ExUnit,
and runs the shared CLI blacklist self-test.
`--sdk-path /path/to/extracted/package` runs the same tests against a separately
extracted source archive. `--offline` excludes the tests tagged `:tcp`, which
bind loopback sockets; the default includes them.

What the tests cover:

- providers: Mint's HTTP serializer and parser over a fragmented memory
  transport, the shared provider fixtures, replay, byte limits, and
  informational HTTP responses. These do not prove TCP or TLS behavior;
- core: ordered events, immutable results, nested and concurrent runs,
  cancellation, and held cleanup and final delivery;
- sessions: the shared parsed commands, acknowledgement ordering, history
  rollback, reset, self-close, and owner exit;
- native I/O bridge and RPC, through real subprocesses: byte preservation,
  EOF, partial writes, stalled and broken output, exact line and queue limits,
  history commit and rollback, SIGINT, external cancellation, and owner
  cleanup;
- tools: the shared schema cases, invalid construction, immutable arguments,
  annotations, bounded recovery, cancellation, and consumer failures;
- Bash: validation, bounded output, separate streams, cancellation, background
  processes, and owner exit;
- CLI: streaming, replay, recoverable failures, rollback, EOF, and interrupts
  during idle input, requests, Bash, and stalled output;
- over TCP (omitted by `--offline`): both providers, and the HTTP server's
  framing, IPv4/IPv6 authentication, byte bounds, ordered events, history
  rollback, independent sessions, silent disconnects, cancelled stalled
  writes, deletion, and shutdown.

Python only drives the native subprocess tests; it is not a runtime dependency.
`test/test_helper.exs` limits ExUnit to two concurrent groups because several
groups launch additional BEAM runtimes.

The direct check leaves a compiled SDK in `_build/direct`, which the CLI
entrypoint can use:

```sh
elixir -pa _build/direct/lib/tiny_agent_sdk/ebin tiny_agent.exs --self-test
```

Last verified in September 2026 on macOS arm64, and not covered by CI. At that
time `mix test`, the direct check, and Hex packaging passed. Linux and
execution on Elixir 1.15 are unverified.
