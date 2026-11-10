# TinyAgent C# SDK

This .NET 8 package provides native tool runs with OpenAI Responses and Anthropic
Messages, ordered events, immutable completed history, cancellation, and JSONL
RPC. The optional `TinyAgent.Http` package adds native Kestrel HTTP sessions.
The imported SDK, coding CLI, RPC, and HTTP share one sequential agent loop.
Schemas use JsonSchema.Net 8.0.5 and its MIT-licensed dependencies. This version
precedes the maintenance-fee agreement added to later binary releases.
The dependency graph is recorded in NuGet lockfiles.

Status: experimental reference implementation. Sessions are held in memory:
durable sessions, resume, recovery and compaction are not implemented, so the
"Durable sessions and compaction" section of
[PROTOCOL.md](../../PROTOCOL.md) does not apply here. These packages are not in
CI. Their checks were last run in September 2026 on macOS arm64 and have not
been re-run since.

## Run an agent

```csharp
using TinyAgent;

string model = Environment.GetEnvironmentVariable("TINYAGENT_MODEL")
    ?? throw new InvalidOperationException("Set TINYAGENT_MODEL.");
using var provider = new OpenAIProvider();
using var cancellation = new CancellationTokenSource();
var agent = new Agent(provider, model, "Answer concisely.");

RunResult result = await agent.RunAsync(
    "Explain this project.",
    onEvent: (value, token) =>
    {
        if (value is AgentEvent.TextDelta text)
            Console.Write(text.Delta);
        return ValueTask.CompletedTask;
    },
    cancellationToken: cancellation.Token);

Console.WriteLine();
Console.WriteLine($"Messages: {result.History.Count}");
```

Set `TINYAGENT_MODEL` and `OPENAI_API_KEY` before running this example; it sends a
real provider request. Pass `result.History` into the next `RunAsync` call to
continue a conversation. `Agent` keeps immutable configuration. Concurrent runs
on one agent have separate histories, cancellation scopes, IDs, and sequences.
The caller owns the provider and disposes it after awaiting its runs.

The event callback is awaited in order. `AgentEvent` has typed start, text delta,
tool start/end, completed, failed, and aborted variants; `ToJson()` creates a fresh object in the
shared wire format. Callback failures propagate without wrapping and stop further
delivery. Provider failures and cancellation emit a terminal event when the
callback remains usable. Unknown tool names fail the run. When a provider returns text without deltas, the core emits that text once as a `TextDelta`.

Run cancellation and provider deadlines reach text callbacks through their
supplied token. Start callbacks receive the run token, and terminal callbacks
receive the caller's token. A terminal callback can still deliver an aborted
event when that token is already cancelled. Callbacks must finish or cooperate
with cancellation; the core awaits them and asynchronous cleanup. Cancellation
during terminal delivery prevents returning a completed result. Failed and
cancelled runs leave the supplied history unchanged.

## Handle RPC commands

`RpcSession` maps TinyAgent's four commands onto one native session. This example
handles `get_state` without making a provider request:

```csharp
using System.Text.Json.Nodes;
using TinyAgent;

string model = Environment.GetEnvironmentVariable("TINYAGENT_MODEL")
    ?? throw new InvalidOperationException("Set TINYAGENT_MODEL.");
using var provider = new OpenAIProvider();
await using var session = new RpcSession(new Agent(provider, model), (value, token) =>
{
    Console.WriteLine(value.ToJsonString());
    return ValueTask.CompletedTask;
});

await session.HandleAsync(new JsonObject { ["id"] = "state", ["type"] = "get_state" });
```

`HandleAsync` accepts detached JSON commands for `prompt`, `abort`, `new_session`,
and `get_state`. A prompt is admitted before the method returns, while its
acknowledgement and run execute asynchronously. The acknowledgement finishes
before the first event or provider request. `WaitForIdleAsync()` awaits the
observed run through final delivery and cleanup. Only a successful run commits
history; failed delivery, cancellation, and cleanup failure retain prior history.
Separate sessions can share one configured agent.

The emitter receives a non-null cancellation token for run acknowledgements and
events. It must await their complete delivery, serialize concurrent writes, and
cooperate with cancellation. The token may already be cancelled when delivering
an aborted event. Ordinary command replies have no token and may enter a bounded
transport queue so a slow peer does not prevent reading abort commands.

`Cancel()` signals the active run and returns its ID. `Stop()` also closes
admission. `DisposeAsync()` cancels and waits for cooperative cleanup. Reset
clears history and waits for the old run before accepting another prompt. From
inside a run callback, reset and disposal request cancellation without awaiting
that callback's own return; `WaitForIdleAsync()` returns `false` there. An
external owner must still await cleanup before disposing the provider.

`RpcSession` handles parsed commands and protocol objects. Use `Rpc` when the
SDK should also own framing, byte limits, output queues, and I/O cleanup.

## Run JSONL RPC

Compile this host, then run it with redirected stdin and stdout on Linux or
macOS:

```csharp
using TinyAgent;

string model = Environment.GetEnvironmentVariable("TINYAGENT_MODEL")
    ?? throw new InvalidOperationException("Set TINYAGENT_MODEL.");
using var provider = new OpenAIProvider();
Environment.ExitCode = await Rpc.RunStdioAsync(new Agent(provider, model));
```

The host reads the commands in [PROTOCOL.md](../../PROTOCOL.md). State, abort,
and reset do not require credentials; prompts use the configured provider.
`RunStdioAsync` owns stdin/stdout pipes or regular files, cancels on EOF or
Ctrl-C, and awaits cleanup before returning. It returns exit code 130 for
Ctrl-C and zero for EOF. Windows and terminal-attached stdio are not supported
by this adapter. The caller retains ownership of the provider.

`Rpc.RunAsync(agent, input, output, cancellationToken)` accepts caller-supplied
streams on any .NET platform. It takes ownership of both streams, which must
honor cancellation of pending asynchronous I/O. It disposes them after its
reader, writer, session, and cancellation callbacks finish. Caller cancellation
raises `OperationCanceledException`; transport failures propagate after cleanup.
Protocol limit failures expose a stable `RpcException.Code`.

Input and output objects are limited to 1 MiB, excluding LF. Queued output is
limited to 4 MiB, including LF and the current write. Run acknowledgements and
events await complete delivery; other command replies queue so input can still
reach abort or EOF while stdout is slow. Oversized output emits a protocol error
and cancels the active run. Queue overflow keeps the current frame, discards
pending frames, and attempts to deliver an `output_queue_full` error before
closing. A cancelled write has one second to drain. Successful draining permits
another run; a stalled write raises `output_stalled` and closes the connection.

The stdio adapter uses owned native pipe handles. Borrowing these handles can
leave disposal waiting on a cancelled operation on the tested .NET 8.0.7
runtime; ownership lets cancellation and disposal finish together. Regular
files use `FileStream`. No native interop shim or additional package is needed.

## Serve HTTP sessions

Install `TinyAgent.Http` and the ASP.NET Core 8 runtime to host the five
baseline routes in [PROTOCOL.md](../../PROTOCOL.md). The SDK package alone does not require
ASP.NET Core.

```csharp
using TinyAgent;

string model = Environment.GetEnvironmentVariable("TINYAGENT_MODEL")
    ?? throw new InvalidOperationException("Set TINYAGENT_MODEL.");
using var provider = new OpenAIProvider();
await using var server = await HttpServer.StartAsync(
    new Agent(provider, model), new HttpServerOptions { Port = 8080 });
Console.WriteLine(server.Address);
Console.ReadLine();
```

The server binds to `127.0.0.1` by default. Port zero chooses an available port;
`Address` reports the bound address. `Host` accepts a literal IP address or
`localhost`. Non-loopback addresses require both `AllowRemote = true` and a
nonempty `Token`. Tokens use printable, non-space ASCII and authenticate every
route with `Authorization: Bearer <token>`. CORS is absent.

Each session owns independent history and permits one active prompt. JSONL
events await delivery; failed delivery or cancellation retains prior history.
Disconnecting a prompt cancels its run. Deletion closes admission, cancels the
run, and waits for cooperative cleanup. Request bodies and individual JSONL
objects are limited to 1 MiB; an oversized event becomes a bounded terminal
`output_too_large` error and cannot commit history.

`RequestStop()` closes admission and starts cancellation. It is safe inside a
provider callback. An external owner must await `DisposeAsync()` before
disposing the provider. Shutdown and cancelled output allow one second for
transport draining, then abort the connection while still awaiting cooperative
run cleanup. Calling and awaiting `DisposeAsync()` from the run being stopped
would wait on itself; use `RequestStop()` there. The server installs no process
signal handlers. The example exits when stdin receives a line or reaches EOF.

## Run guarded Bash

The SDK and coding CLI share `Bash.RunAsync` on Linux and macOS:

```csharp
using TinyAgent;

using var cancellation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
Console.Write(await Bash.RunAsync("printf 'hello\\n'", cancellation.Token));
```

It returns the existing `exit_code`, `stdout`, and `stderr` observation, including
ordinary nonzero exits. Stdin is closed. Output, including the observation
envelope, is limited to 131072 UTF-8 bytes. Overflow stops the command and raises
`IOException`; caller cancellation raises `OperationCanceledException` after
cleanup. Invalid output bytes become Unicode replacement characters and still
count against the final byte limit.

Bash job control puts each command in its own process group. Cancellation kills
that group, and normal completion also stops background descendants still in
it. Commands that need background output should wait for their jobs. The
supervisor suppresses its own job-status messages and ignores ambient Bash
startup files/options. This is process cleanup, not isolation: a command can
deliberately escape its process group. The shared blacklist remains a heuristic,
not a sandbox. `Bash.DeniedReason` reports why a command is blocked.

The coding CLI registers this executor as a native `Tool` on its configured Agent.

## Build and check

From this directory, with the .NET 8 SDK and Python 3 installed:

```sh
python3 check.py
python3 check.py --offline
```

The helper builds the library, the checks, and the CLI with warnings treated as
errors. It creates `artifacts/TinyAgent.Sdk.0.0.1.nupkg` and
`artifacts/TinyAgent.Http.0.0.1.nupkg`, runs the native checks in
`tests/TinyAgent.Checks/`, and checks the CLI blacklist, with provider
credentials removed. Restore uses NuGet's public feed with locked dependencies.
Set `TINYAGENT_NUGET_SOURCE` to a local feed for cached packages. Neither
command calls a paid provider.

The full check includes four groups that bind loopback TCP sockets. `--offline`
omits them. The remaining checks exercise:

- providers: native `SocketsHttpHandler` serialization, HTTP parsing, and SSE
  parsing over fragmented memory streams;
- RPC: real subprocess pipes and file redirection;
- HTTP: Kestrel and `SocketsHttpHandler` connected through bounded in-memory
  pipes, covering framing, authentication, concurrent sessions, cancellation,
  disconnects, failed final chunks, and held shutdown cleanup;
- Bash: native execution and process-group cleanup;
- CLI: the same CLI source with the SDK's streaming provider over a fixture
  `HttpClient`, plus native subprocesses for Ctrl-C during idle input, HTTP,
  Bash, and stalled output.

The RPC subprocess and Bash checks require Linux or macOS. A passing
`--offline` run says nothing about TCP behavior.

Last verified in September 2026 on macOS arm64, and not covered by CI. At that
time the full check passed, including the four TCP groups. Linux and Windows
are unverified.

Use the local NuGet package from a separate .NET project:

```sh
# Configure both the local artifact directory and nuget.org in NuGet.Config.
dotnet add package TinyAgent.Sdk --version 0.0.1
# For HTTP hosting, this also installs the SDK dependency:
dotnet add package TinyAgent.Http --version 0.0.1
```

Each package contains its compiled library, README, and MIT license. Checks and
the original CLI are excluded. Version 0.0.1 has not been published to a registry.

## Stream a provider turn

```csharp
using TinyAgent;

string model = Environment.GetEnvironmentVariable("TINYAGENT_MODEL")
    ?? throw new InvalidOperationException("Set TINYAGENT_MODEL.");
using var provider = new OpenAIProvider();
using var cancellation = new CancellationTokenSource();
List<Message> history = [new UserMessage("Explain this project.")];

ProviderTurn turn = await provider.StreamAsync(
    new ProviderRequest(model, "Answer concisely.", history),
    (delta, token) =>
    {
        token.ThrowIfCancellationRequested();
        Console.Write(delta);
        return ValueTask.CompletedTask;
    },
    cancellation.Token);

history.Add(turn.Assistant);
```

Set `OPENAI_API_KEY` before running this example. Use `new AnthropicProvider()`
with `ANTHROPIC_API_KEY` and an Anthropic model for Messages. Credentials resolve
when a request starts. Importing the library and constructing a provider do not
need credentials. Running this example sends a real provider request.

`ProviderRequest` contains a model, instructions, neutral message history, and
optional `ToolDefinition` metadata. `ProviderTurn` exposes text, complete
`ToolCall` argument objects, provider replay data, and an `AssistantMessage` for
the next request. `ToolResultMessage` carries the matching call ID and output.
Tool metadata does not validate a schema or execute a handler.

Requests and returned values copy their collections. JSON values are cloned
`JsonElement` snapshots, so disposing the caller's `JsonDocument` cannot
invalidate them. Replay retains OpenAI reasoning items and Anthropic thinking
signatures and usage. Keep replay with the same provider.

Text callbacks are awaited in order. The default 180-second deadline covers
headers, body reads, callback delivery, and cleanup. Callbacks must return or
cooperate with their cancellation token. Completion, cancellation, and callback
failure release the response before `StreamAsync` returns. Cancellation that
arrives during asynchronous response cleanup still prevents a successful turn.

Providers accept an endpoint, timeout, and optional caller-owned `HttpClient`.
The default client disables redirects and cookies and closes unread responses
without draining them. An injected client retains its own configuration and is
never disposed by the provider. Dispose a provider after awaiting its requests.
Disposing it while requests are active requests cancellation; callers still own
awaiting those tasks. Cancellation raises `OperationCanceledException`; request
failures and deadlines raise `ProviderException`.

## Structure

- `src/TinyAgent/` holds the native Agent, immutable event/result values, the two
  providers, bounded SSE parsing, and HTTP lifetime management. `Session` owns
  committed history and one active run; `RpcSession` maps protocol commands onto
  it. `Rpc` owns the JSONL connection, `RpcOutput` bounds serialized output, and
  `Stdio` opens native process handles for RPC and the CLI. Each run and provider request owns its
  state. `Bash` provides the shared blacklist and bounded process-group executor.
- `src/TinyAgent.Http/` owns the native Kestrel listener, HTTP routes, request
  validation, and streamed response delivery. It shares the core `Session` with
  RPC and does not duplicate the agent loop.
- `tests/TinyAgent.Checks/` contains native checks and transport fixtures.
- `cli/TinyAgent.Cli.csproj` builds the original `tiny_agent.cs` on .NET 8.

The coding CLI uses `Agent.RunAsync`, typed immutable history,
provider replay, and the SDK's Bash executor. Text is displayed as it arrives;
terminal text without deltas is also displayed. All returned Bash calls execute
sequentially. Invalid arguments and ordinary execution errors become recoverable
observations. Failed delivery or cancellation retains preceding history.

During a turn, Ctrl-C cancels provider work, Bash, or a pending pipe write and
returns exit code 130 after cleanup. Idle Ctrl-C uses the console's default exit
behavior. Redirected Linux/macOS pipes use cancellable owned handles shared with
RPC; regular files and `/dev/null` are supported. Terminal input keeps normal
console handling. The imported CLI runner leaves caller-supplied streams and
the provider open. The CLI only renders events and reads user input; `Agent` owns
provider requests, validation, tool execution, and completed history.

Run the coding CLI or its blacklist self-test with:

```sh
dotnet run --project cli/TinyAgent.Cli.csproj
dotnet run --project cli/TinyAgent.Cli.csproj -- --self-test
```

## Define a tool

```csharp
using System.Text.Json;
using TinyAgent;

var echo = new Tool("echo", "Return the supplied text.",
    JsonSerializer.Deserialize<JsonElement>("""
        {"type":"object","properties":{"text":{"type":"string"}},
         "required":["text"],"additionalProperties":false}
        """),
    (arguments, token) =>
    {
        token.ThrowIfCancellationRequested();
        return ValueTask.FromResult(arguments.GetProperty("text").GetString()!);
    });
using var provider = new OpenAIProvider();
var agent = new Agent(provider, "fixture-model", tools: [echo]);
Console.WriteLine(echo.Definition.Name);
```

This example constructs the agent without sending a provider request. Use a real
model when calling `RunAsync`. The handler receives an immutable JSON snapshot
and the run's cancellation token. `Tool.Definition` exposes its provider metadata.
Schemas compile once at construction. Draft 2020-12 validation neither fetches
external schemas nor changes input; `format` is an annotation. Invalid schemas,
unresolved references, and cycles that do not descend into input are rejected.
Local recursive references remain supported.

Tools run sequentially. Invalid arguments, ordinary handler exceptions, invalid
Unicode, and outputs above 131072 UTF-8 bytes become bounded error observations.
Cancellation propagates after handler cleanup and emits a matching tool end event
when the consumer remains usable. Custom handlers must cooperate with cancellation.
Tool side effects are not rolled back. Completed history includes each assistant
tool call and matching `ToolResultMessage`.

The coding CLI also exposes the shared transports:

```sh
dotnet run --project cli/TinyAgent.Cli.csproj -- --mode rpc
dotnet run --project cli/TinyAgent.Cli.csproj -- serve
```

State and health operations work without credentials. Prompts use the CLI's
configured OpenAI model and guarded Bash tool. Server options use
`TINYAGENT_HOST`, `TINYAGENT_PORT`, `TINYAGENT_TOKEN`, and
`TINYAGENT_ALLOW_REMOTE=1`. Ctrl-C closes the listener and awaits active runs.
