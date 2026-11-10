# Implementation notes from the original eleven languages

Status: recorded 2026-09-11, before the first release narrowed to Python and
TypeScript. This page keeps the per-language detail that used to live in the
top-level README. It describes the in-memory baseline each language reached.
Swift and Ruby have since been removed from the repository, so their sections
and links below are historical. It has not been re-verified since, and the test counts below are from that
date. For the supported implementations, the current state is in the
[README](../README.md) and each package's own README.

## SDK packages

The Python, TypeScript, Go, Rust, Zig, C#, Ruby, PHP, Java, and Elixir implementations are also programmatic packages
documented in [`sdk/`](../sdk/README.md). Each SDK and its CLI use one native agent
loop. The [Go SDK](../tinyagents/go/README.md) also shares session history and
cancellation between its RPC and HTTP adapters.

The [Rust SDK](../tinyagents/rust/README.md) has an importable async core, validated
tools, both streaming providers, cancellable Bash, RPC, and HTTP. Its CLI and
both transports import the same core and share session ownership.

The [Zig SDK](../tinyagents/zig/README.md) targets Zig 0.16.0 and adds text runs,
both streaming providers, validated sequential tools, ordered events, owned replay history, sessions, and
native JSONL RPC and HTTP sessions. Cancellation events and native task cancellation join cleanup.
Checks cover the core, providers, session ownership, bounded output, and RPC
subprocesses, including real pipes and provider sockets. Thirteen HTTP socket
groups verify routes, authentication, limits, history, and connection cleanup.
Tool schemas use a statically linked validator built with Cargo; the agent loop
and handlers remain Zig. Guarded Bash captures bounded output and joins process-group
cancellation. Its coding CLI, RPC mode, and HTTP mode use the shared Agent and session.

The [Java module](../tinyagents/java/README.md) provides validated native tools,
both streaming providers, ordered events, immutable history, and cancellation.
NetworkNT 3.0.7 validates schemas and arguments. The coding CLI, RPC, and Jetty
HTTP server share one Agent loop and session owner. All 77 SDK and 11 CLI groups
pass from source and an installed Maven package. Five README examples compile;
offline examples and installed CLI RPC/HTTP startup and cleanup pass.

The [Ruby gem](../tinyagents/ruby/README.md) provides both streaming providers,
validated native tools, ordered events, immutable history, and run cancellation.
Its RPC adapter owns committed session history and bounded native pipe output.
`TinyAgent.serve` adds authenticated HTTP sessions through WEBrick and shares
the same session owner. Its coding CLI, RPC, and HTTP modes share one tool loop
with the imported SDK. JSONSchemer 2.5.0 checks schemas and arguments; guarded
Bash remains bounded and cancellable. All 93 source and installed-gem tests pass,
including eleven HTTP groups. Six examples pass syntax checks; Tool, Bash, and
HTTP examples execute. CLI RPC/HTTP startup and shutdown checks pass.

The [C# package](../tinyagents/csharp/README.md) provides native async tool runs,
both streaming providers, typed events, immutable history, and cancellation.
Its RPC adapter adds bounded JSONL output and cooperative shutdown over owned
streams or redirected Linux/macOS stdio. The optional `TinyAgent.Http` package
hosts authenticated Kestrel sessions and shares the same session owner. All 95
groups pass from source and isolated NuGet packages, including native subprocess
and HTTP framing checks. All seven README examples compile. The SDK, CLI, RPC,
and HTTP modes share one validated tool loop and bounded, cancellable Bash execution.
Native CLI checks cover Ctrl-C during HTTP, Bash, and stalled output, plus broken pipes.
JsonSchema.Net 8.0.5 validates schemas and arguments. All four TCP groups pass.

The [PHP package](../tinyagents/php/README.md) provides validated native tools,
typed events, immutable completed history, both streaming providers, and scoped
cancellation. Opis JSON Schema 2.6.0 checks schemas and arguments. The coding
CLI, RPC, and ReactPHP HTTP server share one Agent loop and session driver.
All 97 source and independent Composer checks pass, including eleven HTTP groups
and both provider TCP groups. Seven README examples pass syntax checks;
Tool, Bash, session, RPC, and HTTP examples execute without paid requests.
Installed CLI RPC/HTTP startup and cleanup also pass. Linux execution remains
unverified.

The [Elixir package](../tinyagents/elixir/README.md) provides validated native tools,
typed events, both Mint streaming providers, immutable history, and cancellation.
JSV 0.22.0 checks schemas and arguments. The coding CLI, RPC, and Bandit HTTP
server share one Agent loop and session controller. All 136 source and independent
Mix tests pass with bounded schedulers, including fourteen HTTP groups and both
provider TCP groups. Eight examples compile; offline examples and installed CLI
RPC/HTTP startup and cleanup pass. Earlier subprocess timeouts and platform
limits remain recorded in the finish contract.

The [Swift package](../tinyagents/swift/README.md) provides native async text runs
and both providers for Apple platforms, with typed events, immutable history,
and task cancellation. `RpcSession` adds parsed commands, committed session
history, reset, and cooperative shutdown. Native RPC adds bounded JSONL over
owned handles and macOS stdio, with EOF, Ctrl-C, and broken-pipe cleanup.
`HttpServer.start` adds authenticated HTTP sessions through SwiftNIO and the same
session actor. The coding CLI shares the streaming provider, typed history, and
bounded, cancellable Bash executor. All 81 groups pass from source and a separate
SwiftPM consumer, which also runs the HTTP README example.
Validated tools and their loop and CLI integration with `Agent` remain open.
The finish contract records package and platform acceptance.

Python and TypeScript separate provider parsing and SSE transport from their
agent cores. Shared provider fixtures exercise tool loops, cancellation,
bounds, and replay metadata through native HTTP response readers. Package
checks cover Python's installed wheel and TypeScript's installed tarball.

All eleven SDKs request encrypted OpenAI reasoning with `store: false` and
retain the returned metadata for subsequent requests. Request-and-replay checks
cover the opt-in as well as preserving the response.

Python also separates RPC from its core. Its lifecycle checks cover pending
acknowledgements, terminal delivery, history rollback, and tool cleanup. JSONL
checks cover short writes, strict JSON numbers, and bounded output. Native
subprocess checks verify EOF and Ctrl-C, stalled and failed stdout, pipelined
commands, and Bash process-group cleanup, including from the installed wheel.

The [finish contract](../FINISH_CONTRACT.md) tracks the remaining runtime work and
the evidence required to close the project.

## HTTP server

Python, TypeScript, Go, Rust, Zig, C#, Ruby, PHP, Java, and Elixir expose a configured agent through an in-memory session server:

```sh
node tinyagents/typescript/tiny_agent.ts serve
.venv/bin/python tinyagents/python/tiny_agent.py serve
go run tinyagents/go/tiny_agent.go serve
cargo run --manifest-path tinyagents/rust/Cargo.toml -- serve
tinyagents/zig/zig-out/bin/tinyagent serve
dotnet run --project tinyagents/csharp/cli/TinyAgent.Cli.csproj -- serve
ruby tinyagents/ruby/tiny_agent.rb serve
php tinyagents/php/tiny_agent.php serve
java -cp 'tinyagents/java/target/tiny-agent-0.1.0.jar:tinyagents/java/target/dependency/*' tinyagents/java/TinyAgent.java serve
./tinyagents/elixir/tiny_agent.exs serve
```

The default address is `127.0.0.1:8080`. Set `TINYAGENT_TOKEN` to require bearer
authentication on every route. Python, TypeScript, C#, Ruby, PHP, Java, and Elixir read `TINYAGENT_HOST`,
`TINYAGENT_PORT`, and `TINYAGENT_ALLOW_REMOTE=1`. Go, Rust, and Zig use `serve --host`, `--port`,
and `--allow-remote`. Non-loopback binding requires explicit opt-in and a token.
Remote deployments need HTTPS termination at a trusted reverse proxy.

C# also exposes `HttpServer.StartAsync(agent, options)` through its optional
`TinyAgent.Http` package. Ruby exposes `TinyAgent.serve(agent, **options)` through
its gem, Java provides `HttpServer.start(agent, options)`, and PHP provides
`HttpServer::start($agent, ...)`. Swift provides `HttpServer.start(agent:...)`.
Elixir provides `HttpServer.start_link(agent, options)`.
Swift still needs coding CLI integration. Zig provides
`HttpServer.start(allocator, io, agent, options)` and integrates it with its CLI. All eleven HTTP
adapters are implemented, and their source socket tests pass on macOS.
Package verification and final acceptance remain tracked in the finish contract.

Python and TypeScript have native HTTP fixtures that verify response completion,
history rollback, cancellation, authorization, and body limits over bounded
in-memory byte streams. These checks also pass against their installed packages.
The separate TCP suites remain mandatory.

## Runtime design

[`PROTOCOL.md`](../PROTOCOL.md) defines the native core, JSONL RPC mode, and
authenticated HTTP server contract. Python, TypeScript, Go, Rust, and Zig implement the
streaming core, neutral history, cancellation, OpenAI Responses, and Anthropic
Messages. Their coding CLIs default to OpenAI and select Anthropic with
`TINYAGENT_PROVIDER`. All five expose RPC and share fixtures under
[`conformance/`](../conformance/). C#, Ruby, PHP, Java, and Elixir now share their native tool loops with
their coding CLIs and both transports too. Swift still
needs validated tools and shared CLI loops; the finish contract tracks each one.

Run `.venv/bin/python conformance/run.py` to check all native SDKs. Missing
runtimes and unfinished implementations produce a nonzero exit status. Pass
language names, such as `python typescript`, to run a focused check.

