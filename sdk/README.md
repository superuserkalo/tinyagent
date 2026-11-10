# TinyAgent SDK 0.0.1

This page is the cross-language reference for the in-memory core that all ten
SDK languages share: tools, providers, events, RPC and HTTP. It was last
verified in September 2026 and its test counts are from then.

Python and TypeScript have since added `Session`, with durable storage, resume,
compaction and recovery. Those are documented in the
[Python guide](../tinyagents/python/README.md), the
[TypeScript guide](../tinyagents/typescript/README.md) and
[PROTOCOL.md](../PROTOCOL.md), which are the current references for the two
supported implementations. The other languages remain experimental.

Python, TypeScript, Go, Rust, Zig, C#, Ruby, PHP, Java, and Elixir have native programmatic tool-running SDKs.
Each shares that core with its coding CLI. There is no second agent loop under `sdk/`.

## API

The implemented core surface has five concepts:

- `Tool`: a function name, description, JSON Schema, and native handler.
- `Provider`: the model adapter interface. `OpenAIProvider` and `AnthropicProvider` implement streaming model calls.
- `Agent`: a model, instructions, provider, and tools.
- `AgentEvent`: an ordered lifecycle event with a run ID and sequence number.
- `RunResult`: final text, provider-neutral replayable history, and the final raw provider output.

Python and TypeScript expose both `Agent.run` and `Agent.stream`. Go emits events
through `RunOptions.OnEvent` while `Agent.Run` executes. Rust's
`run_with_events` awaits an event callback. Each core adds neutral
user history, asks the provider for streamed deltas and completed tool calls,
runs tools sequentially, and repeats until the model answers. Provider replay
metadata preserves OpenAI reasoning items and Anthropic thinking signatures.

All OpenAI SDK providers request `include: ["reasoning.encrypted_content"]`
with `store: false`, so returned reasoning can be replayed on later calls.
Python and TypeScript's injected complete-response transports receive the same
option. Provider tests inspect request bodies as well as retained replay metadata.

The `Agent` core has no enabled tools by default. The guarded Bash tool used by each standalone CLI is an example, not a safe execution boundary.

Python and TypeScript export `RpcSession`, a programmatic single-session adapter,
and `run_rpc` or `runRpc` for TinyAgent RPC v1 over JSONL streams. Go exposes
`RunRPC`, and Rust exposes `run_rpc`. Each adapter allocates a run ID before acknowledging a prompt and
commits only completed history. Direct callers can also supply a run ID.

## Python

Install the canonical agent as a package:

```sh
python3 -m pip install ./tinyagents/python
```

```python
from tinyagent import Agent, Tool

echo = Tool(
    name="echo",
    description="Return text.",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
    handler=lambda arguments: arguments["text"],
)

agent = Agent(
    model="gpt-5.6",
    instructions="Use tools when useful.",
    tools=[echo],
)
result = agent.run(
    "Echo hello",
    on_event=lambda event: print(event.data["delta"], end="")
    if event.type == "text_delta" else None,
)
print(result.text)
```

Run its checks from the repository root:

```sh
python3 -m unittest discover -s tinyagents/python -p 'test_*.py'
python3 tinyagents/python/tiny_agent.py --self-test
```

Use a virtual environment when your Python installation requires one.
`tiny_agent.py` contains the core and CLI. `tinyagent_types.py` holds shared
values and cancellation, `tinyagent_rpc.py` owns RPC commands and sessions, and
`tinyagent_rpc_io.py` owns pipe I/O and the bounded output queue.
`tinyagent_http.py` owns HTTP sessions and listener lifetime;
`tinyagent_http_body.py` parses bounded JSON request bodies.
`tinyagent_openai.py` and `tinyagent_anthropic.py` own provider serialization
and replay; `tinyagent_sse.py` handles bounded SSE framing and HTTP lifetime. The small
`tinyagent.py` module preserves the public package import name.

Python's `RpcSession.close()` and `new_session` wait for pending prompt
acknowledgement and run cleanup. An emitter that calls `close()` on its own run
requests cancellation; cleanup finishes as that callback returns. The session
stays busy until terminal delivery finishes, and failed or cancelled delivery
retains the previous history. Emitters may accept a second `CancellationToken`
argument for acknowledgements and events. A `cancellation=` token on `RpcSession`
also cancels pending acknowledgement and provider work.

RPC command replies enter a queue bounded to 4 MiB, so command input can keep
reading abort and EOF while output is slow. Agent events wait for delivery;
`agent_start` holds provider work behind the prompt acknowledgement and
`agent_end` holds completed history behind the terminal write. Queue overflow
emits `output_queue_full`, cancels the run, and ends the connection. Cancelled
native writes have one second to drain. A connection that drains within that
period stays usable after an abort.

The driver retries short writes, rejects non-JSON numeric constants, and reports
transport errors after cleanup. Native descriptors use nonblocking I/O, then
regain their prior blocking modes. Callers retain ownership of the streams.
The default Ctrl-C handler records cancellation for the I/O loop and is restored
on exit; custom signal handlers remain with the embedding application. Native
pipe and process-group checks run on POSIX. Custom streams without a native
descriptor use synchronous methods, which must return for cleanup to advance.

Python provider constructors accept an optional `endpoint` and `timeout`
and resolve credentials when a request starts. A 180-second deadline remains
active during response parsing. Cancellation shuts down the active socket
before closing its buffered reader, so a blocked read can finish cleanup.
Connection establishment uses the standard library's socket timeout.

Use Anthropic by supplying its native provider and `ANTHROPIC_API_KEY`:

```python
from tinyagent import Agent, AnthropicProvider

agent = Agent(model="claude-sonnet-4-6", provider=AnthropicProvider())
```

## TypeScript

Install or build from `tinyagents/typescript`:

```sh
cd tinyagents/typescript
npm ci --ignore-scripts
npm run check
npm test
npm run build
```

```typescript
import { Agent, Tool } from "tiny-agent-sdk";

const echo = new Tool({
  name: "echo",
  description: "Return text.",
  parameters: {
    type: "object",
    properties: { text: { type: "string" } },
    required: ["text"],
    additionalProperties: false,
  },
  handler: ({ text }) => text,
});

const agent = new Agent({
  model: "gpt-5.6",
  instructions: "Use tools when useful.",
  tools: [echo],
});
const result = await agent.run("Echo hello", [], {
  onEvent(event) {
    if (event.type === "text_delta") process.stdout.write(event.delta);
  },
});
console.log(result.text);
```

`tiny_agent.ts` contains the core and the directly executable CLI. `http.ts`
adapts that core to Node's HTTP server, and `protocol.ts` holds shared byte limits.
`rpc.ts` owns the JSONL driver and RPC session lifecycle. `types.ts` holds shared
values and tool validation. `openai.ts` and `anthropic.ts`
own provider serialization and replay; `sse.ts` handles bounded SSE framing and
HTTP lifetime. The compiler emits all eight modules and rewrites their local
TypeScript imports for the installed package.

Both providers accept optional `endpoint`, `timeoutMs`, and `fetch` options.
Credentials resolve when a request starts. The default 180-second deadline
remains active while reading the body and delivering text callbacks. Completion,
cancellation, or consumer failure cancels the response reader and awaits its
cleanup. Shared fixture tests inject native `Response` byte streams; separate
mandatory TCP tests exercise real client connections.

Run either standalone implementation in RPC mode with `--mode rpc`. Commands and events use one UTF-8 JSON object per line, and stdout contains no interactive prompts.

TypeScript's RPC driver stops input and cancels the run when output fails or
closes. An oversized command response emits a bounded protocol error and
cancels the active run while allowing subsequent commands. Closing a pending
output write also releases its provider; listeners installed by the embedding
application remain intact.

Cancelled RPC writes have one second to drain. A stalled transport then closes
so provider and tool cleanup can finish. A connection that drains within that
period remains usable. The CLI waits for driver cleanup before exiting, including
when Node retains a blocked stdout descriptor. Embedding callers retain ownership
of process lifetime when using `runRpc`.

Custom `RpcSession` emitters receive an optional second `AbortSignal` argument
for prompt acknowledgements and run events. They should use it to release
blocked writes. `RpcSession.close()` waits for acknowledgement cleanup as well
as the active run.

Use Anthropic by supplying its native provider and `ANTHROPIC_API_KEY`:

```typescript
import { Agent, AnthropicProvider } from "tiny-agent-sdk";

const agent = new Agent({
  model: "claude-sonnet-4-6",
  provider: new AnthropicProvider(),
});
```

## Go

The [Go package guide](../tinyagents/go/README.md) covers its public interface,
local module installation, and CLI. `NewAgent` accepts a native `Provider` and
zero or more tools. `Run` receives a `context.Context`, a prompt, and `RunOptions`
containing prior history and an optional event callback. Tool handlers decode
validated `json.RawMessage` arguments into concrete Go types.

`RunRPC` and `Serve` share one private session implementation. `Serve` returns
an `HTTPServer` with `URL`, `Close`, and `Wait` methods. Go's coding CLI also
imports this core; its standalone file is only an entrypoint.

## Rust

The [Rust crate guide](../tinyagents/rust/README.md) covers the native async core,
providers, tools, CLI, RPC, and HTTP. `Agent::run` accepts owned history and a
`CancellationToken` through `RunOptions`. `run_with_events` adds an async
callback without changing the loop. `Tool::new` validates arguments before
calling a native async handler.

`run_rpc` accepts a configured `Arc<Agent<P>>` and Tokio input/output streams.
It commits completed history and waits for cooperative cleanup on EOF or
cancellation. Dropping its future cancels the active run. The CLI selects either
streaming provider and uses the same core with the cancellable Bash tool.

`serve(Arc<Agent<P>>, HttpOptions)` returns an `HttpServer` with `url`,
`close`, and `wait` methods. Options contain a typed `SocketAddr`, a bearer
token, and `allow_remote`. RPC and HTTP use the same private session module.
Rust serves one HTTP/1 request per connection and waits for its terminal response
to finish writing before committing history. Dropping the server requests
shutdown; awaiting `close()` also waits for cooperative cleanup.

## Scope

Zig's [SDK module](../tinyagents/zig/README.md) exposes `Agent.run`, a native
provider function and context, typed events, owned results, and neutral history.
It uses Zig 0.16.0 task cancellation. Callbacks finish before the run returns;
callback failure or cancelled terminal delivery cannot return completed history.
Results copy nested provider metadata and earlier history into their own arena.
`OpenAIProvider` and `AnthropicProvider` stream through native HTTP and preserve
reasoning items, thinking signatures, and complete tool arguments for replay.
Requests close on terminal events, cancellation, callback failure, or timeout.
`Session` owns committed history, reserves one run, and provides prompt cancellation,
reset, stop, and cleanup waits. Callback cancellation is checked by the core;
session requests and native task cancellation share one driver. Returned results
and committed history have independent ownership. `runRpc` adds bounded native
JSONL over borrowed files. Prompt acknowledgements and events await serialized
delivery; cancellation and pipe failure join cleanup. The embedding application
routes process signals through a cancellation event. Native and subprocess
groups cover the core, providers, sessions, output, RPC, and HTTP. `HttpServer.start`
adds authenticated native HTTP sessions around the same session owner. Final
JSONL and connection-close framing finish before history commits. `Tool.init`
checks Draft 2020-12 schemas through a statically linked native validator built
with Cargo. The Zig loop runs handlers sequentially, bounds observations, and
pairs tool events during cancellation. The SDK needs no Rust installation at
runtime. `bashTool` supplies guarded native Bash with bounded output and joined
process-group cancellation. The console, RPC mode, and HTTP CLI mode all use the
shared Agent and session. The original entrypoint now builds with Zig 0.16.0.

Java's [SDK module](../tinyagents/java/README.md) exposes `Agent.run`, typed
events, completed results, immutable history, both streaming providers, and
cancellation. `Rpc.runStdio` and `Rpc.run` add native JSONL commands, bounded
output, committed session history, and cleanup on EOF or interruption. The
channel adapter owns its input/output channels; the caller owns the provider.
`Bash.run` supplies bounded output and process-group cancellation, shared by the
coding CLI alongside the streaming provider and typed history. `HttpServer.start`
adds authenticated HTTP sessions through Jetty and the same session owner used
by RPC. `Tool` compiles immutable Draft 2020-12 schemas through NetworkNT 3.0.7.
Agent executes native handlers sequentially, bounds errors, and pairs tool events
during cancellation. The coding CLI, RPC mode, and HTTP mode share that loop.
All 77 SDK and 11 CLI groups pass from source and an installed Maven package.
Five README examples compile; offline examples and CLI RPC/HTTP modes execute.

Ruby's [SDK gem](../tinyagents/ruby/README.md) adds validated `Tool` handlers,
`Agent.run`, and `RunResult` to its streaming providers. Runs execute tools
sequentially, emit ordered frozen events, and return immutable completed history.
Callbacks execute synchronously on the
caller's thread and fiber; early exits and failures close the native reader.
`run_rpc` and `RpcSession` add native JSONL commands, committed session history,
and bounded output with subprocess cancellation checks. `Bash.run` supplies
bounded output and process-group cancellation, shared by the coding CLI alongside
the streaming provider and frozen neutral history. `TinyAgent.serve` adds
authenticated HTTP sessions through WEBrick and the same session owner used by
RPC. All 93 source and installed-gem tests pass. The gem contains sixteen runtime
files. JSONSchemer 2.5.0 validates immutable schemas and arguments. The coding
CLI, RPC mode, and HTTP mode share the tool loop. Six README examples pass syntax
checks, and the Tool, Bash, and HTTP examples execute without provider requests.

C#'s [SDK package](../tinyagents/csharp/README.md) exposes `Agent.RunAsync`, typed
events, completed results, both async streaming providers, and native
cancellation. Callbacks, response disposal, and cancellation cleanup finish
before a result returns. `RpcSession` maps parsed protocol commands onto shared
session ownership, with history commit after final delivery and cleanup.
`Rpc.RunAsync` owns cancellable streams; `Rpc.RunStdioAsync` adds redirected
Linux/macOS pipes and files, bounded JSONL output, EOF, and Ctrl-C cleanup.
The optional `TinyAgent.Http` package adds `HttpServer.StartAsync` with native
Kestrel parsing, bounded JSONL, independent sessions, bearer authentication, and
disconnect/shutdown cleanup. It shares `Session` with RPC. `Bash.RunAsync`
provides bounded output and process-group cancellation, shared by the coding CLI
alongside the streaming provider and typed history. Native CLI checks verify
Ctrl-C during HTTP, Bash, and stalled output, plus broken-pipe cleanup.
All ninety-five groups, including validated tool loops and native TCP checks,
pass against isolated NuGet packages. All seven README examples compile; both
RPC examples, Bash, HTTP, and tool construction also run.
The SDK and original CLI target .NET 8; HTTP additionally uses the ASP.NET Core
shared runtime. `Tool` compiles immutable Draft 2020-12 schemas using the pinned
MIT-licensed JsonSchema.Net 8.0.5. `Agent` validates arguments, runs handlers
sequentially, bounds errors, and pairs tool events during cancellation. The coding
CLI, RPC mode, and HTTP mode all use that loop.

PHP's [SDK package](../tinyagents/php/README.md) exposes synchronous `Agent::run`,
typed events, immutable completed results, and both streaming providers.
Callbacks and provider cleanup finish before the run returns. Failed or
cancelled final delivery cannot return a result. Child tokens keep callback
cancellation scoped to its run, and separate fibers retain independent state.
Returned JSON objects are copied, and message arrays detach PHP references.
`RpcSession` maps parsed commands onto one explicitly advanced fiber and its
committed history. It keeps admission closed through run cleanup and prevents
failed or cancelled final delivery from committing. `Rpc::runStdio` and `Rpc::run`
add bounded native POSIX JSONL over owned streams with PCNTL signal handling.
The driver keeps reading commands while run output waits for delivery, joins
provider cleanup, and closes its streams on return. cURL network waits yield
inside SDK-owned session fibers; direct provider calls remain synchronous.
`Bash::run` provides bounded output and process-group cleanup, shared with the
coding CLI alongside the streaming provider and typed history. `HttpServer::start`
adds authenticated HTTP sessions through ReactPHP and the same session driver.
`Tool` validates immutable Draft 2020-12 schemas and arguments through Opis
JSON Schema 2.6.0. Agent runs handlers sequentially, bounds error observations,
and pairs tool events during cancellation. The coding CLI, RPC, and HTTP modes
share that loop. All 97 source and independent Composer groups pass. The ZIP
contains thirty runtime PHP files and offline schema resources. Seven examples
pass syntax checks; Tool, Bash, session, RPC, and HTTP examples execute. Installed
CLI RPC/HTTP startup and cleanup pass. Linux execution remains unverified.

Elixir's [SDK package](../tinyagents/elixir/README.md) exposes synchronous
`Agent.run`, typed events, immutable completed results, and both streaming
providers. Callbacks run in the caller's process; provider cleanup and final
delivery finish before a result returns. Each run owns its history, cancellation,
and temporary callback state. Mint owns HTTP/1 serialization and parsing; the
SDK owns SSE, replay, and request cleanup. `RpcSession` maps parsed commands onto
a linked controller that owns history and one monitored run. Acknowledgement
precedes provider entry; completed history commits after final delivery and run
exit. Reset and close await cooperative cleanup, including held callbacks.
`Rpc.run_stdio` and `Rpc.run` add bounded native JSONL over borrowed descriptors,
EOF and interrupt cleanup, and cancellation while output is stalled. An internal
POSIX bridge owns native pipe I/O, descriptor cleanup, and scoped SIGINT handling.
`Tool.new` validates immutable Draft 2020-12 schemas through JSV 0.22.0.
Agent executes native handlers sequentially, bounds observations, and pairs
tool events during cancellation. The CLI, RPC mode, and Bandit HTTP mode share
that loop. All 136 source and independent Mix tests pass with bounded schedulers,
including fourteen HTTP groups and both provider TCP groups. Eight examples
compile and the offline examples and CLI RPC/HTTP modes execute. The Hex package
contains twenty-five Elixir runtime sources, two native C helpers, and their
build script. Earlier subprocess timeouts, Linux execution, and minimum-version
execution remain recorded in the finish contract.

Swift's [SDK package](../tinyagents/swift/README.md) exposes `Agent.run`, typed
events, immutable completed results, and both async streaming providers through
`URLSession.AsyncBytes`. Each run owns a child task, transcript, and event
sequence. Callbacks and provider cleanup finish before the run returns.
`RpcSession` maps parsed commands onto shared actor-owned history and task
lifetime. It acknowledges prompts before events, commits after final delivery,
and awaits cooperative cleanup on reset or close. `Rpc.run` owns native handles;
`Rpc.runStdio` adds macOS stdin/stdout and temporary SIGINT handling. Both use
bounded JSONL output and await cancellation cleanup. `HttpServer.start` adds
authenticated HTTP sessions through SwiftNIO and the same session actor.
The CLI shares the streaming provider, typed history, and guarded Bash executor.
All 81 groups pass from source and a separate SwiftPM consumer, including ten
HTTP socket groups and both provider TCP groups. Its HTTP README example also
builds and runs in that consumer. The Swift 6.2 package contains twenty runtime files and targets
macOS 13 and iOS 16; Bash, the CLI, and `runStdio` are macOS-only.
The finish contract records package and platform acceptance. Validated tools
and their loop and CLI integration remain open.
The CLI tool loop remains outside `Agent` until executable tools are implemented.

The `Agent` core is streaming and holds no conversation of its own. Runs accept a run-scoped `CancellationToken` in Python or `AbortSignal` in TypeScript. Tool handlers receive that cancellation signal, and the example Bash tool terminates its child process group when cancelled. Python providers and tools are synchronous. TypeScript providers are async iterators and tools may be synchronous or asynchronous.

`Agent.run` closes the stream if an event callback fails. When consuming Python
`Agent.stream` directly, use `contextlib.closing` if you may stop before its
terminal event. Python's `for` statement does not close an iterator on `break`.
TypeScript's `for await` loop closes its iterator automatically.

Both interfaces check cancellation after the completed event is delivered and
before returning history. Cancelling during that delivery prevents a result.
Throwing into a stream suspended at its terminal event preserves the caller's
exception without emitting a second terminal event.

`Agent` itself has no persistence; in Python and TypeScript, `Session` adds it. There are no framework adapters, middleware, approval UI, or built-in command execution in `Agent`.

Tool parameters use JSON Schema Draft 2020-12. Validation checks the schema at
construction and each argument object before calling a handler. Local references
are supported; external schemas are never fetched. `format` is an annotation.
Validation does not coerce values, add defaults, or strip properties. Invalid
arguments become recoverable tool errors. Python uses `jsonschema`, TypeScript
uses Ajv, Go uses `kaptinlin/jsonschema`, and Rust uses the `jsonschema` crate.
Zig statically links that validator through a three-function native interface.
C# uses JsonSchema.Net; Ruby uses JSONSchemer; PHP uses Opis JSON Schema; Java uses NetworkNT; Elixir uses JSV. Their shared cases live in
`conformance/tool_arguments.json`.

## HTTP

`serve` uses the tools and provider already configured on an `Agent`:

```typescript
import { Agent, serve } from "tiny-agent-sdk";

const server = await serve(new Agent({ model: "gpt-5.6" }), {
  port: 8080,
  token: process.env.TINYAGENT_TOKEN,
});
console.log(server.url);
```

Call `await server.close()` when the embedding application stops.

Python exposes the same routes through a background server:

```python
from tinyagent import Agent, serve

server = serve(Agent(model="gpt-5.6"), port=8080)
print(server.url)
# Keep the embedding process alive while serving requests.
# Call server.close() during application shutdown.
```

Python also supports `with serve(agent) as server:` for scoped ownership. Its
options are `host`, `port`, `token`, and `allow_remote`.

Ruby's `TinyAgent.serve(agent, **options)` accepts the same option names and
returns a server with `url`, `stop`, `wait`, and `close`. WEBrick owns HTTP
parsing; the existing session worker writes JSONL directly after response
headers finish. Each connection serves one request with an unchunked response.
Call `stop` inside a provider callback, then `wait` or `close` from the external
owner to join cleanup. See the [Ruby example](../tinyagents/ruby/README.md#http-sessions).

Java's `HttpServer.start(agent, options)` returns a server with `url`, `stop`,
`awaitStopped`, and `close`. Its `Options` record accepts a resolved address,
an optional token, and `allowRemote`. Jetty owns HTTP/1 framing; the shared
session worker awaits every write, including terminal framing. See the
[Java example](../tinyagents/java/README.md#http-sessions).

PHP's `HttpServer::start($agent, ...)` binds a native listener and returns its
`url`. `run()` drives a private event loop and the existing session fibers until
shutdown finishes. `stop()` requests shutdown; an external `close()` joins it.
ReactPHP owns HTTP/1 framing. Events wait for the native socket buffer to empty,
including terminal framing, before history commits. See the
[PHP example](../tinyagents/php/README.md#http-sessions).

Swift's `HttpServer.start(agent:host:port:token:allowRemote:)` returns an actor
with `url`, `stop`, and `close`. SwiftNIO owns HTTP/1 framing; the shared session
awaits each native write and the final framing before committing history.
Call `stop()` from a callback and `close()` from the external owner to join
cleanup. See the [Swift example](../tinyagents/swift/README.md#http-sessions).

Elixir's `HttpServer.start_link(agent, options)` returns a linked server process.
`url(server)`, `stop(server)`, and `close(server)` expose its lifetime. Bandit owns
HTTP/1 framing; each event waits for native delivery through the existing session
worker. A separate reader observes silent EOF and the current callback's
cancellation token. See the [Elixir example](../tinyagents/elixir/README.md#http-sessions).

`host` defaults to `127.0.0.1`; `port: 0` requests an available local port. A
non-loopback host requires `allowRemote: true` and a nonempty `token`. Configured
tokens protect every endpoint, including `/health`. No CORS headers are added.

Zig's `HttpServer.start(allocator, io, agent, options)` returns a server with
`url`, `stop`, `wait`, and `deinit`. The standard HTTP parser owns request framing;
each event awaits native output and the final event finishes connection-close
framing. `stop` is safe in callbacks. The external owner uses `wait` or `deinit`
to join cooperative cleanup. See the [Zig example](../tinyagents/zig/README.md#http-sessions).

Create a session with `POST /sessions`. Send `{"message":"hello"}` as
`application/json` to `POST /sessions/:id/prompts` and consume the returned JSONL
events. `POST /sessions/:id/cancel` cancels a run. `DELETE /sessions/:id` cancels
and releases a session. Disconnecting the prompt response cancels its run.
Closing the server cancels all active runs and waits for cooperative cleanup.

HTTP output is bounded. Python, TypeScript, and Go await event writes; Rust
uses a one-event channel and waits for the terminal write to finish. An oversized event becomes a terminal
`output_too_large` error. Failed or aborted runs retain the last completed
history. Custom tools must honor cancellation; completed side effects cannot
be undone.

Python and TypeScript commit history only after the terminal response, including
its HTTP framing, finishes writing. Cancelled writes have one second to drain
before the response closes. This deadline applies to pending writes; a tool may
finish its cooperative cleanup while a healthy response stays open. Authenticated
requests must contain exactly one Authorization header, and cancellation rechecks
session state after reading the request body. Deletion retains the active run
until cleanup finishes, so concurrent server shutdown also waits for it.

Python and TypeScript each have eight server socket tests and two provider TCP
tests. Their offline provider checks use native HTTP response readers with
fragmented byte streams.
Both also run native HTTP server/client framing and lifecycle checks over bounded
in-memory paired streams, including final-write failure and stalled readers.
Go has handler tests plus
real socket suites for routes, cancellation, disconnects, and backpressure. Rust
tests Hyper HTTP/1 framing over bounded in-memory streams and has three separate
TCP checks for the SDK server and CLI.

C# has twelve native Kestrel/HttpClient groups over bounded pipes, including
malformed HTTP framing, final-chunk failure, stalled delivery, and shutdown
that retains cooperative provider cleanup after transport cancellation. These
groups also pass against its optional HTTP NuGet package. Its four mandatory
TCP groups cover both providers, the SDK server, and executable tool sessions. Ruby's eleven HTTP socket groups
cover routing, framing, authentication, limits, history, cancellation, stalled
delivery, disconnects, and shutdown; they also pass against the installed gem.
Java adds ten HTTP socket groups and both provider TCP groups against a separately
installed Maven package. Its README example also compiles and runs in that consumer.
PHP adds eleven HTTP socket groups and both provider TCP groups against a separately
installed Composer package. They cover stalled terminal delivery after provider
cleanup, cancellation and disconnect rollback, and deletion during shutdown.
Swift adds ten HTTP socket groups covering native framing, limits, history,
cancellation, stalled output, silent EOF, deletion, and callback shutdown. They
also pass in a separate SwiftPM consumer alongside all other native groups.
Elixir adds fourteen HTTP socket groups, including provider child-token cancellation,
held cleanup, creating-process exit, and closing immediately after terminal framing.
Zig adds thirteen HTTP socket groups, including delayed cancellation bodies,
held cleanup, stalled native writes, and client EOF during final delivery.
All eleven HTTP implementations now pass their source and independent-package TCP
checks on macOS. Final acceptance is tracked separately.
See [the finish contract](../FINISH_CONTRACT.md) for current verification.
