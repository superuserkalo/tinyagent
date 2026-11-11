# TinyAgent SDK reference

This page is the cross-language reference for the protocol version 1
in-memory core: the part of TinyAgent that all nine languages have. It lists
what each language provides, the names of its entry points, and the rules they
share.

Python and TypeScript implement protocol version 2, which adds a storage
interface, resume, pause, replayable events, hooks, typed output, limits,
retries, a third provider, images and approval. This page does not describe
those. For version 2 read [PROTOCOL.md](../PROTOCOL.md), the
[Python guide](../tinyagents/python/README.md) and the
[TypeScript guide](../tinyagents/typescript/README.md).

There is no code under `sdk/`. Each implementation lives in
`tinyagents/<language>/` and shares one agent loop with its coding CLI, its RPC
mode and its HTTP server.

Read this page with:

- [PROTOCOL.md](../PROTOCOL.md), the version 2 contract. Its opening says
  which parts the version 1 implementations have.
- The per-language guides linked in the table below, which hold the full
  examples and build instructions.

## Languages

| Language | Status | Runtime | Sessions |
| --- | --- | --- | --- |
| [Python](../tinyagents/python/README.md) | Supported, in CI, protocol version 2 | Python 3.10 or newer | Any `Storage`; memory and JSONL file stores included |
| [TypeScript](../tinyagents/typescript/README.md) | Supported, in CI, protocol version 2 | Node.js 22.18 or newer | Any `Storage`; memory and JSONL file stores included |
| [Go](../tinyagents/go/README.md) | Experimental | Go 1.26.4 | In memory |
| [Rust](../tinyagents/rust/README.md) | Experimental | Cargo; checked with Rust 1.96.1 | In memory |
| [Zig](../tinyagents/zig/README.md) | Experimental | Zig 0.16.0, plus Cargo to build | In memory |
| [C#](../tinyagents/csharp/README.md) | Experimental | .NET 8 | In memory |
| [Java](../tinyagents/java/README.md) | Experimental | Java 21 | In memory |
| [PHP](../tinyagents/php/README.md) | Experimental | PHP 8.4, Composer | In memory |
| [Elixir](../tinyagents/elixir/README.md) | Experimental | Elixir 1.15 or newer, C compiler | In memory |

Python and TypeScript are the supported implementations and are at protocol
version 2. Everything they add beyond this page is in
[PROTOCOL.md](../PROTOCOL.md) and their guides.

The other seven are experimental reference implementations of the version 1
in-memory core and report `protocolVersion: 1`. They implement tools, the
OpenAI Responses and Anthropic Messages providers, events, cancellation, RPC
and the five baseline HTTP routes. They do not persist sessions, resume, pause,
replay or compact, and they are not in CI.

## Concepts

- `Tool`: a name, a description, a JSON Schema for the arguments, and a native
  handler.
- `Provider`: the model adapter. Every language has an OpenAI Responses
  provider and an Anthropic Messages provider, both streaming, and accepts a
  custom provider. Python and TypeScript also have a Chat Completions provider.
- `Agent`: a model, instructions, a provider and tools. It holds no
  conversation. A run takes a prompt and prior history and returns final text
  and the new history.
- Events: ordered values carrying a run ID and a sequence number that starts
  at one. The version 1 types are `agent_start`, `text_delta`,
  `tool_execution_start`, `tool_execution_end` and `agent_end`. PROTOCOL.md
  lists their payloads and the fields version 2 adds.
- Run result: final text, provider-neutral replayable history, and the final
  raw provider output.
- `Session`: one conversation. In Python and TypeScript it is a public class
  that owns history and journals it through a `Storage`. Zig exports an in-memory
  `Session`. In the other languages the in-memory session sits behind the RPC
  and HTTP adapters; SDK callers use `RpcSession` where it exists, or pass
  history between runs themselves.

Every loop does the same thing: append the user prompt, ask the provider for
streamed text and complete tool calls, validate and run the tools one at a
time, append the observations, and repeat until the model answers without a
tool call. A failed or cancelled run returns no result and leaves the caller's
history unchanged.

## Entry points

| Language | Run an agent | Receive events |
| --- | --- | --- |
| Python | `Agent(model=..., tools=[...]).run(prompt, history)` | `on_event=` callback, or iterate `Agent.stream` |
| TypeScript | `new Agent({ model, tools }).run(prompt, history, options)` | `options.onEvent`, or iterate `agent.stream` |
| Go | `NewAgent(AgentConfig{...})`, then `agent.Run(ctx, prompt, RunOptions{})` | `RunOptions.OnEvent` |
| Rust | `Agent::new(AgentConfig { .. })?`, then `agent.run(prompt, RunOptions).await` | `agent.run_with_events` with an async callback |
| Zig | `Agent{ .provider, .model }`, then `agent.run(allocator, io, prompt, options)` | `options.on_event` |
| C# | `new Agent(provider, model, instructions, tools)`, then `RunAsync(prompt, history, onEvent, cancellationToken)` | `onEvent` delegate |
| Java | `new Agent(provider, model, instructions, tools)`, then `run(prompt, history, cancellation, events)` | the `events` sink |
| PHP | `new Agent($provider, $model, $instructions, $tools)`, then `run($prompt, history:, onEvent:, cancellation:)` | `onEvent` callable |
| Elixir | `Agent.new(provider, model, options)`, then `Agent.run(agent, prompt, options)` | `on_event:` option |

| Language | JSONL RPC | HTTP server | Server handle |
| --- | --- | --- | --- |
| Python | `run_rpc(agent, ...)`, `RpcSession` | `serve(agent, host=, port=, token=, allow_remote=, directory=, storage=, budget=, allowed_hosts=, allowed_origins=)` | `url`, `close()`, context manager |
| TypeScript | `runRpc(agent, input, output, options)`, `RpcSession` | `await serve(agent, { host, port, token, allowRemote, directory, storage, budget, allowedHosts, allowedOrigins })` | `url`, `close()` |
| Go | `RunRPC(ctx, agent, input, output)` | `Serve(agent, HTTPOptions{Address, Token, AllowRemote})` | `URL()`, `Close()`, `Wait()` |
| Rust | `run_rpc(agent, input, output, cancellation)` | `serve(agent, HttpOptions { address, token, allow_remote })` | `url()`, `close()`, `wait()` |
| Zig | `runRpc(allocator, io, agent, input, output, options)` | `HttpServer.start(allocator, io, agent, options)` | `url`, `stop()`, `wait()`, `deinit()` |
| C# | `Rpc.RunAsync`, `Rpc.RunStdioAsync`, `RpcSession` | `HttpServer.StartAsync(agent, options)` in the `TinyAgent.Http` package | `Address`, `RequestStop()`, `DisposeAsync()` |
| Java | `Rpc.run`, `Rpc.runStdio`, `RpcSession` | `HttpServer.start(agent, options)` | `url()`, `stop()`, `awaitStopped()`, `close()` |
| PHP | `Rpc::run`, `Rpc::runStdio`, `RpcSession` | `HttpServer::start($agent, host:, port:, token:, allowRemote:)` | `url`, `run()`, `stop()`, `close()` |
| Elixir | `Rpc.run`, `Rpc.run_stdio`, `RpcSession` | `HttpServer.start_link(agent, options)` | `url/1`, `stop/1`, `close/1` |

`RpcSession`, where it exists, handles parsed commands for an application that
supplies its own transport. The `run`/`runStdio` functions add JSONL framing,
byte limits and I/O cleanup.

| Language | Schema validator | HTTP server library | CLI providers |
| --- | --- | --- | --- |
| Python | `jsonschema` 4.26.0 | standard library `http.server` | OpenAI, Anthropic, Chat Completions |
| TypeScript | Ajv 8.20.0 | `node:http` | OpenAI, Anthropic, Chat Completions |
| Go | `kaptinlin/jsonschema` 0.9.2 | `net/http` | OpenAI, Anthropic |
| Rust | `jsonschema` crate 0.30.0 | Hyper | OpenAI, Anthropic |
| Zig | the Rust `jsonschema` crate 0.30.0, statically linked ([notes](../docs/zig-schema-validation.md)) | Zig standard library | OpenAI, Anthropic |
| C# | JsonSchema.Net 8.0.5 | Kestrel | OpenAI |
| Java | NetworkNT JSON Schema Validator 3.0.7 | Jetty 12.1 | OpenAI |
| PHP | Opis JSON Schema 2.6.0 | ReactPHP | OpenAI |
| Elixir | JSV 0.22.0 | Bandit | OpenAI |

"CLI providers" is what each coding CLI can select. Every SDK exports the
OpenAI Responses and Anthropic Messages providers regardless. The CLIs that
support more than one read `TINYAGENT_PROVIDER` (`openai` or `anthropic`, and
`chat` in Python and TypeScript). All CLIs read `TINYAGENT_MODEL`.

## Python

```sh
python3 -m venv .venv
.venv/bin/python -m pip install ./tinyagents/python
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

`Agent` uses `OpenAIProvider` unless you pass another provider, such as
`provider=AnthropicProvider()`. Providers accept `base_url`, `headers`, `extra`
and `timeout` (180 seconds by default) and read their API key when a request
starts. Providers and tools are synchronous.
Cancel a run with a `CancellationToken` passed as `cancellation=`.

When you iterate `Agent.stream` or `Session.stream` directly, wrap it in
`contextlib.closing` if you may stop early. A Python `for` loop does not close
an iterator on `break`.

The [Python guide](../tinyagents/python/README.md) covers `Session`, storage,
resume, pause, hooks, typed output and compaction.

## TypeScript

```sh
cd tinyagents/typescript
npm ci --ignore-scripts
npm pack
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

`Agent` uses `OpenAIProvider` unless you pass another provider, such as
`provider: new AnthropicProvider()`. Providers accept `baseUrl`, `headers` and
`extra`, and read their API key when a request starts. Tool handlers may be synchronous
or asynchronous. Cancel a run with an `AbortSignal` passed as `signal`.

The [TypeScript guide](../tinyagents/typescript/README.md) covers `Session`,
storage, resume, pause, hooks, typed output and compaction.

## Tools

Tool parameters use JSON Schema Draft 2020-12 in every language.

- The schema is checked when the tool is constructed. An invalid schema fails
  construction.
- Each argument object is validated before the handler runs.
- Local references are supported. External schemas are never fetched.
- `format` is an annotation. Validation does not coerce values, add defaults
  or remove properties.
- Invalid arguments and ordinary handler failures become tool results with
  `isError: true`, so the model can recover.
- Tool output is limited to 131,072 UTF-8 bytes. Larger output becomes a tool
  error.
- Tools run one at a time. Handlers receive the run's cancellation signal and
  must honor it.
- In Python and TypeScript an unknown tool name is also a recoverable tool
  error, and a tool may be marked `idempotent` or given a timeout.

The shared cases are in
[`conformance/tool_arguments.json`](../conformance/tool_arguments.json).

The guarded Bash tool in each coding CLI is an example tool. Its list of denied
commands is a guardrail, not a sandbox. `Agent` has no tools unless you
register them.

## Providers

Each OpenAI provider requests `include: ["reasoning.encrypted_content"]` with
`store: false`, so reasoning items can be replayed on later calls without
server-side conversation storage. Anthropic providers keep thinking signatures
for the same purpose. This replay metadata is opaque: keep a conversation with
the provider that produced it.

Streaming tool arguments stay inside the provider. The agent loop receives a
tool call only when its arguments form a complete JSON object.

## RPC

Every coding CLI starts RPC mode with `--mode rpc`. It reads one JSON command
per line on stdin and writes responses and events as JSONL on stdout.
Diagnostics go to stderr.

All nine languages implement `prompt`, `abort`, `new_session` and `get_state`.
Python and TypeScript also implement `resume_run`, `abandon_run`,
`get_events`, `get_history`, `compact`, `reconcile`, `list_sessions` and
`resume_session`, and their `get_state` reports `protocolVersion: 2` with
`resumable`, `paused` and `usage`.

A prompt is acknowledged before its first event. Only a completed run whose
terminal event was delivered commits history.

## HTTP

Every coding CLI starts the server with `serve`. All nine languages implement
the baseline routes:

```text
POST   /sessions
DELETE /sessions/:id
POST   /sessions/:id/prompts
POST   /sessions/:id/cancel
GET    /health
```

Python and TypeScript add `GET /sessions`, `GET /sessions/:id`,
`POST /sessions/:id/resume`, `POST /sessions/:id/abandon`,
`GET /sessions/:id/events`, `POST /sessions/:id/compact`,
`POST /sessions/:id/reconcile`, and reopening a stored session through
`POST /sessions`.

Rules common to all servers:

- The default address is `127.0.0.1:8080`. Port 0 selects an available port.
- A non-loopback address requires both an explicit remote opt-in and a bearer
  token. The server refuses to start otherwise.
- A configured token protects every route, including `/health`.
- No CORS headers are added. TLS is not implemented; terminate HTTPS in a
  reverse proxy.
- A prompt response is JSONL: `agent_start` first, `agent_end` last.
  Disconnecting it cancels the run. (In Python and TypeScript a prompt sent
  with `"detach": true` keeps running.)
- Python and TypeScript also refuse a request whose `Host` header does not
  name the server (421) or that carries an `Origin` header not on the allowed
  list (403).
- Closing the server cancels active runs and waits for cooperative cleanup.

The Python, TypeScript, C#, Java, PHP and Elixir CLIs read `TINYAGENT_HOST`,
`TINYAGENT_PORT`, `TINYAGENT_TOKEN` and `TINYAGENT_ALLOW_REMOTE=1`. The Go, Rust
and Zig CLIs take `--host`, `--port`, `--token` and `--allow-remote` after
`serve`, with `TINYAGENT_TOKEN` as the default token.

## Limits

All implementations apply the limits in PROTOCOL.md, measured in UTF-8 bytes:

| Limit | Bytes |
| --- | --- |
| JSONL line, excluding LF | 1,048,576 |
| HTTP request body | 1,048,576 |
| Queued asynchronous output | 4,194,304 |
| Tool output | 131,072 |

## Verification

| Languages | Evidence |
| --- | --- |
| Python | 267 unit tests, and 515 black-box harness cases. CI on Linux and macOS with Python 3.10, 3.12 and 3.14. |
| TypeScript | 206 unit tests, and 515 black-box harness cases. CI on Linux and macOS with Node.js 22 and 24. |
| Go, Rust, Zig, C#, Java, PHP, Elixir | Not in CI. Last verified in September 2026 on macOS arm64 and not re-run since. Linux and Windows are unverified. |

[conformance/README.md](../conformance/README.md) describes the shared
fixtures, the runner and the black-box harness. [STATUS.md](../STATUS.md)
records what is verified for Python and TypeScript and the known limits.
