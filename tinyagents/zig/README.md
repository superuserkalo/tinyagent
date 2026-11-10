# TinyAgent Zig SDK

The Zig SDK supports text runs, OpenAI Responses and Anthropic Messages
streaming, custom providers, schema-checked sequential tools, ordered events,
owned history, sessions, native task cancellation, guarded Bash, JSONL RPC, and
HTTP sessions. The coding CLI and both transports use the same Agent and session.

The SDK requires Zig 0.16.0. Building also requires Cargo and a Rust toolchain;
this checkpoint uses Rust 1.96.1. Schema validation statically links `jsonschema`
0.30.0 through the small `schema-native` library. Its dependencies are locked,
HTTP/file retrieval features are disabled, and runtime lookup rejects external
schemas. Running an SDK executable does not require a Rust installation. The
agent loop, handlers, providers, sessions, and transports are Zig code.
`tiny_agent.zig` owns process signals and dispatches to the SDK's CLI module;
it is included in the source package and requires the same Zig version.

## Build and checks

From this directory:

```sh
zig build test -j1
zig build example
zig build example-tools
zig fmt --check build.zig build.zig.zon tiny_agent.zig src test examples
```

The example prints `You said: Hello` without credentials or network requests.
`zig build` installs that example as `zig-out/bin/tinyagent-offline` and the
offline RPC example as `zig-out/bin/tinyagent-rpc-example`. It also installs
`zig-out/bin/tinyagent-http-example`, which serves until stdin closes.
`zig-out/bin/tinyagent-tools-example` runs an offline provider and an echo tool.
The shared runner also executes these checks:

```sh
python3 ../../conformance/run.py zig
```

The finish contract records package and platform evidence separately from source checks.
The checks require Python 3 for the subprocess harness; the SDK itself has no
Python dependency. `-j1` serializes test executables so separate suites do not
compete with the short subprocess shutdown deadlines. Each lifecycle test still
runs its own concurrent tasks. Seventeen core groups, seventeen provider groups, thirteen
session groups, ten tool groups, four Bash groups, two console/configuration groups,
five output groups, nine CLI subprocess groups, seventeen RPC subprocess groups, and thirteen
HTTP socket groups cover
the implemented runtime without credentials or paid API requests. Provider
checks include real loopback HTTP connections. The package includes the four
shared SSE fixtures and RPC fixture so its checks also run outside this workspace.

## Coding CLI

Build once, then run the executable from the directory where it should work:

```sh
zig build -j1
./zig-out/bin/tinyagent --self-test
./zig-out/bin/tinyagent
./zig-out/bin/tinyagent --mode rpc
./zig-out/bin/tinyagent serve --host 127.0.0.1 --port 8080
```

OpenAI is the default and reads `OPENAI_API_KEY`. Set `TINYAGENT_PROVIDER=anthropic`
and `ANTHROPIC_API_KEY` to use Anthropic. `TINYAGENT_MODEL` overrides the selected
provider's default model. The blacklist self-test needs no credentials. RPC writes
only JSONL to stdout. HTTP startup diagnostics go to stderr; `TINYAGENT_TOKEN`
or `serve --token` configures authentication. Remote binding also requires
`--allow-remote`. Interrupts stop admission and join active work.

The console prints text deltas once and puts Bash commands and observations on
stderr. Its session commits only after final text delivery; failed prompts retain
prior history. Input is valid UTF-8, limited to 1 MiB before LF, and may end with an
unterminated final line. `console.run` also accepts a configured Agent, borrowed
native files, and an optional cancellation event. It preserves file ownership and
modes. Every pending output write has one second to drain after cancellation.

## Import

Add this package as a dependency in your application's `build.zig.zon`. A local
checkout or extracted source archive can be used with a path dependency:

```zig
.dependencies = .{
    .tiny_agent_sdk = .{ .path = "vendor/tiny-agent-sdk" },
},
```

In `build.zig`, import the module into your executable's root module:

```zig
const sdk = b.dependency("tiny_agent_sdk", .{
    .target = target,
    .optimize = optimize,
});
exe.root_module.addImport("tinyagent", sdk.module("tinyagent"));
```

The complete runnable example is [examples/offline.zig](examples/offline.zig).
Its provider and call site are:

```zig
const std = @import("std");
const tinyagent = @import("tinyagent");

fn echo(_: ?*anyopaque, allocator: std.mem.Allocator, io: std.Io, request: tinyagent.ProviderRequest, on_text: tinyagent.TextHandler) !tinyagent.ProviderTurn {
    const prompt = request.history[request.history.len - 1].user;
    const text = try std.fmt.allocPrint(allocator, "You said: {s}", .{prompt});
    try on_text.emit(io, text);
    return .{ .assistant = .{ .text = text } };
}

pub fn main(init: std.process.Init) !void {
    const agent: tinyagent.Agent = .{ .provider = .{ .stream = echo }, .model = "offline" };
    var result = try agent.run(init.gpa, init.io, "Hello", .{});
    defer result.deinit();
    var output = std.Io.File.stdout().writer(init.io, &.{});
    try output.interface.print("{s}\n", .{result.text});
}
```

## Streaming providers

Replace the offline agent's construction with either provider. The embedding
application supplies credentials explicitly, for example from its environment:

```zig
var provider: tinyagent.OpenAIProvider = .{
    .api_key = init.environ_map.get("OPENAI_API_KEY") orelse return error.MissingApiKey,
};
const agent: tinyagent.Agent = .{ .provider = provider.provider(), .model = "gpt-5.6" };
```

```zig
var provider: tinyagent.AnthropicProvider = .{
    .api_key = init.environ_map.get("ANTHROPIC_API_KEY") orelse return error.MissingApiKey,
};
const agent: tinyagent.Agent = .{ .provider = provider.provider(), .model = "claude-sonnet-4-6" };
```

Both providers accept `endpoint` and `timeout_ms`, which defaults to 180,000.
Anthropic also accepts positive `max_tokens`, defaulting to 8192. The native
HTTP client handles TLS and HTTP framing. Redirects are disabled. Each request
closes after its provider terminal event, even if HTTP framing remains open.
Timeout and cancellation remain active while reading headers or SSE data and
while awaiting text callbacks. The request and timer finish before returning.

SSE lines, event data, accumulated tool arguments, and individual reconstructed
text, thinking, and signature buffers are limited to 1 MiB each. Malformed JSON,
invalid UTF-8, incomplete responses, duplicate tool call IDs, and malformed tool
argument objects fail the provider call. Unknown event types are ignored.
OpenAI requests encrypted reasoning with `store: false` and preserves it for
replay. Anthropic thinking signatures and message metadata also survive replay.
Adjacent Anthropic tool results become one user message.

Providers return complete `ToolCall` objects. `Agent` validates and executes them
sequentially, appends their observations, and calls the provider again until it
returns a final answer. Direct provider callers supply an arena and release it after consuming the
returned turn. `Agent.run` manages that arena itself. Provider callbacks execute
in order on the request's Zig task, which may use a different thread from the caller.

## Ownership and events

`Agent` borrows its model, instructions, provider context, function pointer, and tools.
Keep them alive and unchanged until every run has finished. Concurrent runs may
share an agent if the provider supports concurrent calls. Use a thread-safe
allocator when running tasks concurrently with `std.Io.Threaded`.

`Agent.run(allocator, io, prompt, options)` returns a `RunResult`. Call its
`deinit` once. The result owns its run ID, final text, history, and opaque provider
output. It does not borrow from earlier history, prompts, or the provider's result.
Pass `result.history` as `options.history` for a later run. Failed runs return an
error and leave the supplied history unchanged. JSON metadata is copied without
rounding `number_string` values; invalid UTF-8, non-finite numbers, malformed
number strings, and JSON deeper than 128 nested containers are rejected.

Set `options.on_event` to an `EventHandler` with a callback and optional context.
Callbacks run synchronously in order and borrow each event only for the callback's
duration. Copy data you retain. Each event has one run ID and a sequence starting
at one. `std.json.Stringify` serializes events using the shared JSONL protocol's
field names. The caller adds the newline when writing JSONL.

The final callback finishes before a completed result is returned. A callback
error stops further delivery and takes precedence over a subsequent provider
error. Failure to deliver a terminal event cannot return completed history or
emit a second terminal event. Unknown tool calls fail with `UnregisteredTool`.
`ToolDefinition` is provider metadata; `Tool.init` constructs a checked executable tool.

## Tools

[examples/tools.zig](examples/tools.zig) is a complete offline tool run. Construct
a `Tool` with an allocator, `ToolDefinition`, and `ToolHandler`, then register its
address in `Agent.tools`. Names must be unique. Keep every tool and its borrowed
handler context alive until all runs finish, then call each tool's `deinit` once.
The tool owns its schema, name, and description. `definition()` returns borrowed
metadata that callers must not change.

Construction checks Draft 2020-12 schemas, including local and dynamic references.
Invalid schemas and unresolved external references fail immediately. Validation
does not coerce arguments, insert defaults, remove properties, or assert `format`.
Handlers receive their own argument copy, the run's arena, and cancellable `Io`.
Return text allocated in that arena or borrowed text that survives until the
caller resumes. Finish cleanup and propagate `error.Canceled` before returning.
One configured tool can be called by independent runs, so its context must support
any concurrency the embedding application permits.

Invalid arguments, ordinary handler errors, invalid UTF-8 output, and observations
over 128 KiB become bounded error observations in the agent loop. Allocation or
validator failures stop the run. Each started tool gets an end event during
cooperative cancellation, including cancellation from its start callback. A failed
event callback stops delivery and keeps its original error.

Direct `Tool.execute` callers supply a run arena and release it after consuming
the returned text. Schema compilation and validation are synchronous native calls;
run cancellation is checked around validation and interrupts native handler I/O.
The C interface contains only create, validate, and destroy operations. No other
SDK implementation or agent code is linked into this package.

`bashTool(allocator)` constructs the optional guarded Bash tool. Register it in
`Agent.tools` like any custom tool. `runBash(allocator, io, command)` also returns
a caller-owned observation directly. Both streams and the exit code are included
within the 128 KiB output limit. Stdin is closed. The command blacklist preserves
the coding CLI's checks; it is not a sandbox. Cancellation or output overflow
stops the process group on POSIX, including descendants retaining pipes after
the shell exits. Windows stops the direct child. Cleanup joins the native process
wait. The process checks have been executed on macOS arm64.

Native execution is verified on macOS arm64. The build selects Rust targets for
macOS, Linux, and Windows GNU on arm64/x86-64; other targets are rejected. Cross
builds also need the matching Rust target and native linker dependencies. Those
cross-platform builds are not yet verified.

## Sessions

Use a session when the SDK should own conversation history:

```zig
var session = tinyagent.Session.init(init.gpa, init.io, agent);
defer session.deinit();
var first = try session.run("Hello", .{});
defer first.deinit();
var next = try session.run("Again", .{});
defer next.deinit();
std.debug.assert(next.history.len == 4);
```

`Session.run` calls the same agent core. Its returned result and committed
history have independent ownership. Successful final delivery and cleanup
precede the atomic history commit; failures and cancellation preserve the prior
history. `state()` returns a copied optional `run_id`, `message_count`, and
`closed`. Run IDs are fixed 34-byte arrays. Concurrent runs on one session fail
with `SessionBusy`; separate sessions may share a thread-safe provider.

`run` accepts `on_event` and an optional `before_run` callback with context, Io,
and run ID. That callback completes before agent events, allowing a transport
to acknowledge its prompt first. The session remains busy during acknowledgement,
provider work, final delivery, and cleanup. Idle sessions own no background task.

`cancel()` returns the current run ID, or null, and requests cancellation without
waiting. `reset()` also hides committed history immediately and keeps admission
closed until the cancelled run finishes. `stop()` permanently closes admission.
All three are safe from callbacks. An external owner calls `waitIdle(run_id)`
to join that run's cleanup, or `waitIdle(null)` to await idle state. An old run ID
does not wait on a later run. These waits finish cleanup even if their caller is
cancelled.

Keep the session at a stable address, and keep its borrowed agent configuration,
provider, and thread-safe allocator alive. `deinit` stops and joins active work
before releasing history. Stop other callers before deinitializing the session.
Callbacks must use `cancel`, `reset`, or `stop`; calling `waitIdle` or `deinit`
from the active run would wait on itself.

## JSONL RPC

Serve a configured agent over native blocking files:

```zig
try tinyagent.runRpc(init.gpa, init.io, agent, .stdin(), .stdout(), .{});
```

[examples/rpc.zig](examples/rpc.zig) is a complete offline echo server. Send one
UTF-8 JSON command per line, using `prompt`, `abort`, `new_session`, or `get_state`:

```json
{"id":"1","type":"prompt","message":"Hello"}
{"id":"2","type":"get_state"}
```

The input worker reserves the session before accepting the next command. A
single output worker serializes replies and events; prompt acknowledgement
finishes before the first event, and final delivery precedes history commit.
Command replies can queue while output is stalled. Events wait for delivery.
The queue includes complete pending frames and is bounded to 4 MiB. Lines are
bounded to 1 MiB before LF; embedded newlines are escaped. Oversized output
becomes `output_too_large` and cancels its run. Queue overflow terminates the
connection with `output_queue_full`. Malformed JSON is recoverable; invalid
UTF-8 and oversized input terminate the connection after a protocol error.

`options.cancellation` accepts an `std.Io.Event`. Setting it or cancelling the
calling task closes admission, cancels the active run, and joins cleanup. EOF
does the same. Pending cancelled writes have one second to drain; a timeout
returns `OutputDrainTimeout`. A failed pipe interrupts blocked input and also
joins the run. `new_session` waits for the cancelled run before replying.

The caller retains both files and their modes. Use a thread-safe allocator and
an Io implementation with cancellable native file operations, such as
`std.Io.Threaded`. The embedding application owns process signals and should
route Ctrl-C to the cancellation event. The SDK does not install signal handlers;
the subprocess checks exercise this route with a native SIGINT handler.

## HTTP sessions

Start a configured agent on a native listener:

```zig
const server = try tinyagent.HttpServer.start(init.gpa, init.io, agent, .{
    .port = 8080,
    .token = "local-example-token",
});
defer server.deinit();
var output = std.Io.File.stdout().writerStreaming(init.io, &.{});
try output.interface.print("{s}\n", .{server.url});
var input = std.Io.File.stdin().readerStreaming(init.io, &.{});
_ = try input.interface.discardRemaining();
```

[examples/http.zig](examples/http.zig) is a complete offline echo server.
`HttpOptions` accepts a numeric IPv4 or IPv6 `host`, `port`, `token`, and
`allow_remote`. The default
address is `127.0.0.1:8080`; port zero selects an available port. Non-loopback
binding requires both `allow_remote = true` and a token. A configured token
protects every route, including health. Requests require exactly one
`Authorization: Bearer <token>` header. The server adds no CORS headers.

Use `POST /sessions` to create a session, then send an `application/json` object
with a nonempty `message` to `POST /sessions/:id/prompts`. The response contains
ordered JSONL events. `POST /sessions/:id/cancel` requests cancellation without
waiting. `DELETE /sessions/:id` removes the session and joins active cleanup
before returning 204. `GET /health` returns process health and protocol version.

The standard HTTP parser reads fixed-length and chunked requests. Headers are
limited to 16 KiB; bodies and output lines are limited to 1 MiB. Each connection
serves one request. A prompt owns one pending native write and no output queue.
Every event waits for delivery; the terminal event also finishes connection-close
framing before history can commit. Cancelled writes have one second to drain.
Oversized events become a bounded `output_too_large` terminal error. Disconnects,
failed writes, and cancellation retain the previously committed history.

`stop()` closes admission and requests cancellation; it is safe from provider
callbacks. One external owner uses `wait()` to join all connections and release
the listener, or `deinit()` to stop, join, and free the server. Keep the borrowed
agent configuration, thread-safe allocator, and Io alive until then. Native
cancellation interrupts blocked reads and writes. Deletion and shutdown still
wait for cooperative provider cleanup. Application code owns process signals.
Calling `wait()` or `deinit()` from a server callback would wait on itself.

## Cancellation

Pass an `std.Io.Event` to request cancellation without joining in the requesting
callback or task:

```zig
var cancellation: std.Io.Event = .unset;
var running = try io.concurrent(tinyagent.Agent.run, .{
    agent, allocator, io, "Hello", tinyagent.RunOptions{ .cancellation = &cancellation },
});
cancellation.set(io);
if (running.await(io)) |value| {
    var result = value;
    result.deinit();
} else |err| {
    if (err != error.Canceled) return err;
}
```

The agent checks the signal between callbacks and interrupts blocked native
I/O. One owned worker and one cancellation waiter finish before `run` returns.
Completion can win a race with cancellation. Keep the event alive until the
run finishes, and do not reset it while a run is using it. A callback may set
this event; its run then stops before another provider step. Cancellation during
terminal delivery returns an error without a second terminal event. Cancelling
the run's future does not set the caller-owned event.

Without a cancellation event, runs use the caller's Zig I/O task directly.
Start a run with `io.concurrent` when another task must be able to cancel it:

```zig
var running = try io.concurrent(tinyagent.Agent.run, .{
    agent, allocator, io, "Hello", tinyagent.RunOptions{},
});
// At the owner's cancellation point, cancel and join the task.
if (running.cancel(io)) |value| {
    // Completion may have won the race with cancellation.
    var result = value;
    result.deinit();
} else |err| {
    if (err != error.Canceled) return err;
}
```

`Future.cancel` waits for cooperative cleanup. One owner must manage each future;
do not call `await` and `cancel` concurrently on the same future. Providers use
the supplied `io`, propagate `error.Canceled`, and finish their cleanup before
returning. CPU-bound providers call `io.checkCancel()` periodically. Callbacks
that perform I/O also use the supplied `io` so cancellation can interrupt them.
An aborted run emits its terminal event when event delivery is still available.
