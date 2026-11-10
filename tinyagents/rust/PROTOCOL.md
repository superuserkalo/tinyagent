# TinyAgent runtime contract

Status: protocol version 1.

Every implementation follows the core, RPC and HTTP rules in this document with
sessions held in memory. Python and TypeScript, the supported implementations,
also store sessions durably, resume them, and compact them. Those additions are
in [Durable sessions and compaction](#durable-sessions-and-compaction) and are
marked wherever they change a baseline rule. The other languages do not
implement them.

This document defines the contract that every native implementation should expose. It is intentionally small.

## Architecture

Each language has one handwritten agent loop. Four thin adapters may use it:

```text
native Agent core
├── imported SDK
├── plain CLI
├── RPC mode over stdin and stdout
└── HTTP server
```

The SDK and adapters must not contain independent model or tool loops. Python, TypeScript, Go, Rust, Zig, C#, Java, PHP, and Elixir are native implementations. No language runtime is required by another language.

The CLI entrypoint remains runnable where the language permits it. Imported SDKs
and transport adapters share the same native core. Separate modules are useful
when they keep HTTP, RPC, provider code, and the agent loop readable. Generated
agent implementations are outside this project.

## Core contract

Names and syntax should be idiomatic in each language. Behavior should match this contract.

### Agent

An `Agent` is configured with:

- A provider and model.
- Instructions.
- Zero or more tools with unique names.

It supports a complete run and an ordered event stream. A run receives a prompt, prior history, and a run-scoped cancellation signal. A completed run returns final text and replayable history. Provider failures and cancellation use the language's normal error mechanisms.

Cancellation belongs to a run, not globally to an `Agent`, because one configured agent may serve several sessions. A server allows only one active run in each session.

### Provider

A provider adapter converts provider-neutral history and tools into one model API. It emits text deltas and completed tool calls. It must preserve opaque provider metadata required to replay reasoning, signatures, or response identifiers.

Streaming tool arguments remain internal to the adapter. The core receives a tool call only after its arguments form a complete JSON object.

The first provider adapters are OpenAI Responses and Anthropic Messages. Additional providers use the same contract.

### History

History has three logical message kinds:

```text
user          text
assistant     text, reasoning, and tool-call blocks
tool_result   call ID, tool name, output, and error flag
```

Assistant blocks may contain opaque provider JSON needed for lossless replay. Provider switching within an existing session is outside v0.1.

### Tool

A tool has a name, description, JSON Schema, and native handler. The handler receives validated arguments and a cancellation signal. Tools execute sequentially in v0.1.

The schema dialect is Draft 2020-12. Local references are supported. Validation
does not fetch external schemas, coerce values, insert defaults, or remove
properties. `format` remains an annotation. Invalid schemas fail at tool
construction; invalid arguments become recoverable tool observations before
the handler executes. See `conformance/tool_arguments.json` for shared cases.

A normal tool failure becomes a tool result with `isError: true`, allowing the model to recover. Cancellation stops the run. A server executes handlers in its own process. It does not accept executable tool code over HTTP.

The guarded Bash tool remains an optional tool used by the plain coding-agent CLI. Its blacklist is not a sandbox.

## Core events

Every event in a run includes `runId` and a monotonically increasing `sequence` starting at one.

```text
agent_start
text_delta
tool_execution_start
tool_execution_end
agent_end
```

Event payloads are:

```json
{"type":"agent_start","runId":"r_...","sequence":1}
{"type":"text_delta","runId":"r_...","sequence":2,"delta":"Hello"}
{"type":"tool_execution_start","runId":"r_...","sequence":3,"toolCallId":"c_...","toolName":"bash","arguments":{"command":"git status"}}
{"type":"tool_execution_end","runId":"r_...","sequence":4,"toolCallId":"c_...","toolName":"bash","output":"...","isError":false}
{"type":"agent_end","runId":"r_...","sequence":5,"status":"completed","text":"..."}
```

`agent_start` is first and `agent_end` is last. `agent_end.status` is `completed`, `failed`, or `aborted`. Failed events include a stable error code:

```json
{"type":"agent_end","runId":"r_...","sequence":5,"status":"failed","error":{"code":"provider_error","message":"Provider request failed","retryable":true}}
```

Every emitted `tool_execution_start` receives a matching `tool_execution_end` during cooperative cancellation.

Text deltas may occur before and after tool execution. `agent_end.text` contains the final assistant answer, not every text delta emitted during the run.

## RPC mode

RPC mode is started with a flag:

```sh
tinyagent --mode rpc
```

It reads UTF-8 JSONL commands from stdin and writes UTF-8 JSONL responses and events to stdout. JSONL means one complete JSON object followed by LF. Embedded newlines are escaped inside JSON strings. Diagnostics go to stderr so stdout remains machine-readable.

This is a TinyAgent RPC protocol. It is not JSON-RPC 2.0.

### Commands

```json
{"id":"1","type":"prompt","message":"Inspect this repository"}
{"id":"2","type":"abort"}
{"id":"3","type":"new_session"}
{"id":"4","type":"get_state"}
```

IDs are optional strings copied into the corresponding response. Clients should provide them when several commands may be outstanding.

`prompt` starts a run using the current transcript. Its response is written before run events:

```json
{"id":"1","type":"response","command":"prompt","success":true,"data":{"runId":"r_..."}}
```

A prompt received while a run is active fails with `busy`. Prompt queuing and steering are outside v0.1.

`abort` requests cooperative cancellation and reports whether a run was active. `new_session` aborts an active run, clears history, and starts an empty session. `get_state` reports `idle` or `running`, the current run ID, and the message count. Their successful responses are:

```json
{"id":"2","type":"response","command":"abort","success":true,"data":{"accepted":true,"runId":"r_..."}}
{"id":"3","type":"response","command":"new_session","success":true,"data":{"abortedRun":false}}
{"id":"4","type":"response","command":"get_state","success":true,"data":{"protocolVersion":1,"status":"idle","runId":null,"messageCount":0}}
```

`abort.data.accepted` is `false` with a null `runId` when no run is active. It acknowledges the request. The matching `agent_end` event confirms that cancellation finished.

### Errors

Command errors use a stable code and a human-readable message:

```json
{"id":"1","type":"response","command":"prompt","success":false,"error":{"code":"busy","message":"A run is already active"}}
```

Malformed JSON produces a protocol error with code `invalid_json`. An unknown command without a usable command ID produces:

```json
{"type":"protocol_error","error":{"code":"invalid_command","message":"Unknown command type"}}
```

An oversized line or invalid UTF-8 terminates RPC mode. EOF and Ctrl-C cancel the active run and exit without displaying an interactive break menu.

Responses and events may interleave. Responses correlate through `id`. Run events correlate through `runId`.

## HTTP server

Server mode wraps one programmatically configured agent:

```sh
tinyagent serve
```

A native SDK may also expose an idiomatic `serve(agent, options)` function. Tool handlers then execute in the process that called `serve`.

### Endpoints

```text
POST   /sessions
DELETE /sessions/:id
POST   /sessions/:id/prompts
POST   /sessions/:id/cancel
GET    /health
```

`POST /sessions` creates an empty session and returns:

```json
{"sessionId":"s_..."}
```

Each session has independent history and at most one active run.

`POST /sessions/:id/prompts` accepts:

```json
{"message":"Inspect this repository"}
```

A valid prompt returns `200` with `Content-Type: application/jsonl; charset=utf-8`. The body is the same ordered event objects emitted by the core, one per line. The first line is `agent_start` and the final line is `agent_end`.

Validation, authentication, and session errors are returned as ordinary JSON before streaming starts:

```json
{"error":{"code":"session_busy","message":"The session already has an active run","retryable":true}}
```

`POST /sessions/:id/cancel` cooperatively cancels the active run and is idempotent:

```json
{"accepted":true,"runId":"r_..."}
```

It returns `accepted: false` with a null `runId` when no run is active. The prompt stream's `agent_end` event confirms that cancellation finished.

`DELETE /sessions/:id` cancels any active run, waits for it to stop, releases the session, and returns an empty `204` response. `GET /health` returns only process health and protocol version:

```json
{"status":"ok","protocolVersion":1}
```

Python and TypeScript add the routes listed under [Durable sessions and compaction](#durable-sessions-and-compaction).

There is no independent event subscription, replay, or global event stream in v0.1. Disconnecting a prompt response cancels its run. Failed and aborted runs do not commit partial history. Tool side effects cannot be rolled back.

### HTTP status codes

```text
200  success or prompt stream
201  session created
204  session deleted
400  malformed request
401  missing or invalid authentication
404  unknown session or route
409  session already running, already open, closing, or awaiting reconciliation
413  request too large
415  unsupported content type
500  failure before streaming starts
503  server is shutting down
```

Failures after streaming starts are represented by the terminal `agent_end` event because the HTTP status has already been sent.

## Authentication and exposure

Servers bind to `127.0.0.1` by default and do not enable CORS. Non-loopback binding requires both an explicit remote-exposure option and a bearer token. A server must refuse to start remotely without both.

Authenticated requests use:

```text
Authorization: Bearer <token>
```

Session IDs are cryptographically random and are not credentials. One token owns every session in a v0.1 server. Multi-user ownership is outside scope.

Local mode may use HTTP. Remote deployments must terminate HTTPS in a trusted reverse proxy. TinyAgent does not implement TLS itself.

## Durable sessions and compaction

Python and TypeScript only. A session created without a storage directory stays
in memory and behaves as the baseline describes.

### Storage

With a storage directory, each session is one append-only JSONL journal named
after its session ID, plus a lock file that admits one writer at a time. Opening
a session that another process holds fails. Each record carries a version, a
sequence number, and a SHA-256 of its payload.

- A torn final record, left by a crash mid-write, is discarded on open.
- A corrupt earlier record fails the open. The journal is not repaired silently.
- A tool call is recorded as an intent before it runs and as a result after.
  If a process dies between the two, the tool's effects are unknown. The
  session opens with `recoveryRequired: true` and refuses prompts and
  compaction until the client reconciles it. A pending tool is never run again
  automatically.
- Closing or deleting a session releases it. It does not delete the journal.

The coding CLIs store sessions under `./.tinyagent/sessions` in RPC and HTTP
modes. `TINYAGENT_SESSION_DIR` changes the directory.

### Compaction

Compaction replaces older history in the model's context with a summary and
keeps recent complete exchanges. The raw history stays in the journal. The
summary comes from an ordinary model call with tools disabled. A checkpoint is
validated and written before it takes effect, so a failed, cancelled or invalid
summary leaves the previous context in place. Resume restores the last accepted
checkpoint.

Compaction needs a context budget. The CLIs read it from
`TINYAGENT_CONTEXT_TOKENS` and `TINYAGENT_RESPONSE_RESERVE`. With a budget set,
a session also compacts automatically before a request would exceed it.

A compaction run emits two events in place of the usual run events:

```json
{"type":"compaction_start","runId":"r_...","sequence":1}
{"type":"compaction_end","runId":"r_...","sequence":2,"status":"completed","changed":true}
```

`compaction_end.status` is `completed`, `failed`, or `aborted`. `changed` is
`false` when there was nothing to compact.

### RPC commands

```json
{"id":"5","type":"list_sessions"}
{"id":"6","type":"resume_session","sessionId":"s_..."}
{"id":"7","type":"get_history","offset":0,"limit":20,"view":"history"}
{"id":"8","type":"reconcile","note":"Checked: the file was written"}
{"id":"9","type":"compact"}
```

- `list_sessions` returns `{"sessionIds":[...]}`.
- `resume_session` closes the current session, opens the stored one, and
  returns `{"sessionId":"s_..."}`. It needs a storage directory.
- `get_history` returns one bounded page:
  `{"sessionId":"s_...","history":[...],"recoveryRequired":false,"nextOffset":null,"recoveryNotes":[]}`.
  `view` is `history` (committed conversation, the default) or `records` (raw
  durable records, returned under a `records` key). `limit` is 1 to 1000 and
  defaults to 20. `nextOffset` is `null` on the last page.
- `reconcile` records the client's note about interrupted tool effects and
  clears `recoveryRequired`.
- `compact` starts a compaction run. Like `prompt`, it answers with a `runId`
  first and then emits the compaction events.

These four inspection and recovery commands fail with `busy` while a run is
active. `new_session` now closes the current session and opens a new one; the
earlier journal is kept.

RPC error codes added by these commands:

```text
session_error    storage failure, unknown session, missing budget, or a session awaiting reconciliation
session_closed   the RPC adapter has been closed
```

### HTTP routes

```text
GET    /sessions
POST   /sessions                    with {"sessionId":"s_..."} to resume
GET    /sessions/:id?offset=0&limit=20&view=history
POST   /sessions/:id/compact
POST   /sessions/:id/reconcile      with {"note":"..."}
```

`GET /sessions` returns `{"sessionIds":[...]}` for open and stored sessions.
`GET /sessions/:id` returns the same page object as `get_history`.
`POST /sessions/:id/compact` streams the compaction events as JSONL.
`POST /sessions/:id/reconcile` returns `{"recoveryRequired":false}`.
`DELETE /sessions/:id` closes the session and keeps its journal.

HTTP error codes added by these routes:

```text
409  session_busy        the session is already open or has an active run
409  session_closed      the session is closing
409  recovery_required   inspect history, then reconcile interrupted tool effects
400  budget_required     compaction was requested without a context budget
```

## Session and cancellation rules

- Sessions and histories are in memory only, except where Python and TypeScript
  are given a storage directory.
- One session permits one active run.
- Separate sessions may run concurrently.
- A completed run atomically commits its resulting history.
- A failed or aborted run retains the previously committed history.
- Cancellation propagates to the provider request and active tool.
- Built-in Bash cancellation must terminate its child process group where supported.
- Custom tools are responsible for honoring their cancellation signal.
- Server shutdown cancels every active run.

## Limits

Implementations must apply these v0.1 defaults, measured as UTF-8 bytes. The JSONL line limit excludes its LF. The queue limit includes complete framed lines and their LFs:

```text
JSONL line, excluding LF     1,048,576
HTTP request body            1,048,576
queued asynchronous output   4,194,304
tool output                    131,072
```

A synchronous writer has no in-memory output queue. Oversized JSONL input emits `line_too_large` and terminates RPC mode. Invalid UTF-8 emits `invalid_utf8` and terminates. An oversized output object emits `output_too_large` instead of that object and cancels the active run. An oversized tool observation becomes a recoverable tool error, so its full content is never placed in history or an event. Asynchronous output exceeding its queue limit emits `output_queue_full` and cancels the active run. The shared values and RPC scenarios live in [`conformance/rpc_v1.json`](conformance/rpc_v1.json).

## Conformance

Every language implementation must pass the same black-box scenarios:

1. Text-only completion and ordered deltas.
2. One successful tool call.
3. A recoverable tool error.
4. Multiple sequential tool calls.
5. Malformed provider tool arguments.
6. Provider failure before and during streaming.
7. Cancellation during provider streaming and Bash execution.
8. Busy-session rejection.
9. History commit and rollback rules.
10. RPC framing, command correlation, and clean EOF or Ctrl-C shutdown.
11. HTTP authentication, loopback defaults, streaming, and disconnect cancellation.

The current language-specific self-tests remain. Conformance tests supplement them rather than replacing readable local tests.

## Deferred

The following are intentionally outside v0.1:

- ACP compatibility.
- Durable sessions outside Python and TypeScript, and event replay everywhere.
- Multiple configured agents in one server.
- Client-hosted executable tools.
- Parallel tools, steering, and prompt queues.
- Approval UI, workflow graphs, orchestration, and a built-in TUI.
- Built-in TLS and remote user management.
