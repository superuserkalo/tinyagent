# TinyAgent for TypeScript

A small agent SDK you can read in an afternoon. It calls OpenAI Responses and
Anthropic Messages directly, runs your tools, streams what happens, and keeps a
conversation on disk so that it survives a restart.

There are three things to learn:

- **Tool**: a function the model may call, with a JSON Schema for its arguments.
- **Agent**: a model, instructions, and tools. It runs one prompt to completion.
- **Session**: a conversation. It remembers history, saves it, resumes it, and compacts it.

A coding CLI, a JSONL RPC mode, and an HTTP server are included. All three are
thin adapters over the same `Agent` and `Session`. Where a session is kept is
the one extension point: a `Storage` with four operations.

This is protocol version 2. Importing the package, and constructing an `Agent`,
`Tool`, `Session` or storage, reads no environment variable and touches no
file, network or process, unless you passed that resource in (a `directory`,
for example). API keys are read when a request starts.

## Install

TinyAgent needs Node.js 22.18 or later. It is not on the npm registry yet, so
build the package from a clone and install the tarball:

```sh
git clone https://github.com/superuserkalo/tinyagent.git
cd tinyagent/tinyagents/typescript
npm ci --ignore-scripts
npm pack                      # builds dist/ and writes tiny-agent-sdk-0.2.0.tgz

cd /path/to/your/project      # a project with "type": "module"
npm install /path/to/tinyagent/tinyagents/typescript/tiny-agent-sdk-0.2.0.tgz
```

Set `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` before running an agent. Keys are
read when a request starts, never at import.

## A tool and an agent

```typescript
import { Agent, Tool } from "tiny-agent-sdk";

const forecast = new Tool({
  name: "forecast",
  description: "Return tomorrow's forecast for a city.",
  parameters: {
    type: "object",
    properties: { city: { type: "string" } },
    required: ["city"],
    additionalProperties: false,
  },
  handler: ({ city }, signal) => ({ city, sky: "clear", highCelsius: 24 }),
});

const agent = new Agent({
  model: "gpt-5.6",
  instructions: "Answer weather questions with the forecast tool.",
  tools: [forecast],
});

const result = await agent.run("What is the forecast for Lisbon?");
console.log(result.text);
```

Arguments are validated against the schema (JSON Schema Draft 2020-12) before
the handler runs. Invalid arguments, thrown errors, and calls to a tool that
does not exist go back to the model as a tool error, so it can correct itself.
Throw a `ToolError` to choose the exact text the model sees. Handlers may be
async. They receive the run's `AbortSignal` and should stop when it aborts.
Tools run one at a time. Handlers and hooks receive a copy of the call's
arguments, so changing them changes nothing else.

Handlers, hooks, providers and event callbacks should stop when the run is
cancelled. One that does not is abandoned one second after the cancellation:
the run is recorded as aborted and the session is released without it. Only
the run's own signal makes a run aborted; an abort or timeout error that a
tool raises for its own reasons is an ordinary tool error. Calling `close`,
`run`, `resume`, `abandon` or `compact` on a session from inside one of its
own handlers, hooks or event callbacks throws `SessionBusyError` (code
`session_busy`) at once.

The handler's full form is `(args, signal, context)`:

```typescript
const charge = new Tool({
  name: "charge",
  description: "Charge the customer's card.",
  parameters: { type: "object", properties: { cents: { type: "integer" } }, required: ["cents"] },
  idempotent: true,          // safe to repeat: see "Interrupted runs"
  timeoutSeconds: 30,        // fires `signal`, then reports "timed out after 30 s" to the model
  handler: ({ cents }, signal, { executionId, context }) =>
    payments.charge({ cents, idempotencyKey: executionId, signal }),
});
```

`context.executionId` names this execution of the call and stays the same if
the call is run again after a crash, so it works as an idempotency key. In a
session it is `"<sessionId>:<journal sequence of the call>"`; in a bare
`agent.run` it is the model's tool call ID. `context.context` is whatever you
passed as the run's `context` option (a database handle, the current user). It
is never written to the journal or sent to the model. `context.timedOut` is
true once the tool's own `timeoutSeconds` has expired, and `context.signal` is
the same signal as the second parameter. A handler that stops within a second
of its timeout may throw a `ToolError` with what it has; after that the model
is simply told "timed out after N s".

### Limits, usage, and retries

```typescript
const agent = new Agent({ model: "gpt-5.6", tools: [forecast], maxSteps: 20, maxSeconds: 300, maxTokens: 200_000 });
const result = await agent.run("Plan the week.", [], { maxSteps: 5 });   // a run may set its own
console.log(result.usage);   // { inputTokens, outputTokens, requests }, summed over the run
```

`maxSteps` counts model requests, `maxSeconds` is wall time, and `maxTokens`
is input plus output tokens. Each is checked before a model request. A run
that passes one ends with `agent_end` `status: "failed"`,
`error.code: "limit_exceeded"` and `error.limit` (`steps`, `seconds` or
`tokens`), and `run` throws a `RunError` with the same `code` and `limit`. A
run also ends, with `repeated_tool_errors`, after three model turns in a row
in which every tool call failed. `usage` is also on every `agent_end` event.
It holds token counts when the API reports them, and no prices.

A model request that fails with HTTP 429, 500, 502, 503 or 504, or with a
connection error, before any of the response body arrives, is sent again: at
most four attempts, waiting 1, 2 and 4 seconds, or the server's `Retry-After`
when it is a number of seconds, up to 30. A retried request counts once
toward `maxSteps`, and `usage.requests` counts requests that got a response. When the attempts are used up the run fails with `provider_error` and
`error.retryable: true`, meaning the failure was transient and the caller may
try later; after a 4xx such as 401 it is `false`. Other errors, and anything after streaming began, are not
retried. Cancelling the run ends the wait at once. Pass `sleep` to `Agent` to
replace the wait, and `newId` to replace the random part of run and session IDs.

### Hooks, typed output, and per-run options

```typescript
const agent = new Agent({
  model: "gpt-5.6",
  tools: [forecast, charge],
  beforeTool: (call, { context }) =>
    call.name === "charge" && !isAdmin(context) ? { deny: "Only an admin may charge a card." } : undefined,
  afterTool: (call, result) => (call.name === "forecast" && !result.isError ? redact(String(result.output)) : undefined),
});
```

`beforeTool` runs before every tool call, after its arguments are validated.
Return `undefined` to let it run, `{ deny: "reason" }` to give the model that
reason as a tool error, `{ arguments: {...} }` to run it with other arguments
(they are validated again), or `pause(payload)` (see below). `afterTool` is
given every call that ran, as `{ output, isError }`, whether it succeeded or
failed. It is not called for a call that `beforeTool` denied, that paused, or
that was settled as interrupted. Return `undefined` to keep the result, a
string to replace the output, or `{ output, isError }` to replace both; the
replacement is held to the tool output limit. A hook that throws, or returns
something else, ends the run with `hook_error`. A `Tool` can carry a
`beforeTool` of its own.

A run accepts, besides `signal`: `context` (above), `model` and `instructions`
to override the agent's for that run, `extra` (an object merged into the
request body), and `outputSchema`:

```typescript
const result = await agent.run("What is the forecast for Lisbon?", [], {
  outputSchema: { type: "object", properties: { celsius: { type: "number" } }, required: ["celsius"] },
});
result.value;    // { celsius: 24 }, validated
```

With `outputSchema` (a JSON Schema for an object) the agent adds a tool named
`final_output` whose parameters are that schema, and the run completes when
the model calls it with valid arguments. Invalid arguments are an ordinary
tool error the model can correct. If the model answers in text instead, it is
told once to call the tool; a second miss ends the run with `output_missing`.
The value is also `agent_end.value`. `result.output` is unrelated: it holds
the provider's raw output items, as before. There is no provider-specific
structured-output code.

### Images

A prompt is a string, or a list of parts. A tool may return the same list.

```typescript
await session.run([
  { type: "text", text: "What is on this receipt?" },
  { type: "image", mediaType: "image/png", data: base64 },      // or { type: "image", url: "https://..." }
]);

const screenshot = new Tool({
  name: "screenshot", description: "Capture the page.", parameters: { type: "object" },
  handler: async () => [{ type: "text", text: "The checkout page." }, { type: "image", mediaType: "image/png", data: await capture() }],
});
```

The parts are kept as given in history, in the journal and in
`tool_execution_end.output`, and each provider converts them to its API's own
shape. Chat Completions allows only text in a tool message, so a tool's images
follow it in a user message. RPC and HTTP accept the same list as `message`;
their one-megabyte body limit is unchanged, so pass large images by URL.

### Watching model requests

```typescript
const agent = new Agent({ model: "gpt-5.6", trace: true });     // or per run: { trace: true }
```

`trace` adds three events and is off by default, so the usual event sequence
does not change. `model_request_start` has the `attempt` number. Every one is
followed by a `model_request_end` with that request's `usage`
(`{ inputTokens, outputTokens }`), or with `error: { code, message }` and
`willRetry`; `code` is `aborted` when the run was cancelled. `reasoning_delta`
carries the model's reasoning text where the API streams it.

All three providers accept `baseUrl` (the API root; the provider adds `/responses`,
`/messages` or `/chat/completions`), `headers` (extra request headers; one named like the API key
header replaces it) and `extra` (merged into every request body after
TinyAgent's own fields, for temperature, reasoning effort and the like). A
run's `extra` is merged over the provider's.

### Writing a provider

A provider is one method. It receives the model name, instructions,
provider-neutral history and tools, yields text as it arrives, and returns the
complete turn. This one calls an imaginary API that answers in one piece:

```typescript
import { ProviderError, type Provider } from "tiny-agent-sdk";

const provider: Provider = {
  async *stream({ model, instructions, history, tools, signal, extra }) {
    const response = await fetch("https://models.example/generate", {
      method: "POST", signal,
      body: JSON.stringify({ model, system: instructions, messages: history,
        tools: tools.map((tool) => tool.definition()), ...extra }),
    });
    // retryable: true only when nothing of a response arrived and sending again is safe.
    if (!response.ok) throw new ProviderError(`API error ${response.status}`, { retryable: response.status >= 500 });
    const reply = await response.json() as { text: string; calls: { id: string; name: string; input: object }[]; in: number; out: number };
    if (reply.text) yield { type: "text_delta", delta: reply.text };
    const toolCalls = reply.calls.map((call) => ({ callId: call.id, name: call.name, arguments: { ...call.input } }));
    return {
      text: reply.text,
      toolCalls,              // complete, parsed arguments only
      // What is stored in history and sent back to you on the next request.
      assistant: { role: "assistant", content: [{ type: "text", text: reply.text },
        ...toolCalls.map((call) => ({ type: "tool_call", ...call }))] },
      output: [],
      usage: { inputTokens: reply.in, outputTokens: reply.out },
    };
  },
};
```

History items are `{ role: "user", content }`, the `assistant` objects you
returned, and `{ role: "tool_result", callId, toolName, output, isError }`.
Keep anything your API needs for an exact replay inside the assistant object.
Throw when `signal` aborts.

`ScriptedProvider` is the fake model the tests use, exported for yours:

```typescript
import { Agent, ScriptedProvider } from "tiny-agent-sdk";

const provider = new ScriptedProvider([
  [{ callId: "c1", name: "forecast", arguments: { city: "Lisbon" } }],   // a turn of tool calls
  "Clear, 24 degrees.",                                                   // a final answer
]);
const result = await new Agent({ model: "any", tools: [forecast], provider }).run("Lisbon?");
provider.requests[1].history.at(-1);   // the tool result the model was sent
```

A turn may also be `{ text, toolCalls, deltas, usage, error }` or a function
of the request. `provider.requests` holds every request; `provider.remaining`
counts unplayed turns.

To use Anthropic, pass its provider:

```typescript
import { Agent, AnthropicProvider } from "tiny-agent-sdk";

const agent = new Agent({ model: "claude-sonnet-4-6", provider: new AnthropicProvider() });
```

For a gateway or a self-hosted model server that speaks the OpenAI-compatible
`/chat/completions` API, use `ChatCompletionsProvider`:

```typescript
import { Agent, ChatCompletionsProvider } from "tiny-agent-sdk";

const provider = new ChatCompletionsProvider({ baseUrl: "http://localhost:11434/v1", apiKey: "unused" });
const agent = new Agent({ model: "qwen3", provider });
```

It streams text and tool calls, and copes with servers that send no usage.

`agent.run(prompt, history)` is stateless: it returns the new history and keeps
nothing. A session does the keeping for you.

## A session

```typescript
import { Session } from "tiny-agent-sdk";

const session = new Session(agent, { directory: ".tinyagent/sessions" });
await session.run("What is the forecast for Lisbon?");
await session.run("And for Porto?");     // sees the first exchange
console.log(session.id);                 // s_3f0c..., the name to resume by
await session.close();
```

Leave out `directory` for a session that lives only in memory.

### Storage

A session is an ordered list of JSON records. `Session` takes a `storage`
object that keeps them. Two ship with the package: `FileStorage(directory)`
and `MemoryStorage`. `directory` is shorthand for the first, and no storage
option means a private `MemoryStorage`. To keep sessions somewhere else,
implement four operations:

```typescript
import { FencedError, type Item, type Storage } from "tiny-agent-sdk";

class TableStorage implements Storage {
  /** The records in order, each with its `sequence` (1..n), or undefined for an unknown session. */
  async load(sessionId: string): Promise<Item[] | undefined> { /* ... */ }
  /** Store `record` as number `expected + 1`, durably, and return that number. */
  async append(sessionId: string, expected: number, record: Item): Promise<number> {
    // Succeed only if the session holds exactly `expected` records. Otherwise
    // write nothing and throw new FencedError(). `expected` 0 creates the session.
  }
  async list(): Promise<string[]> { /* ... */ }
  /** Let go of connections or locks. Never delete data. */
  async release(sessionId: string): Promise<void> { /* ... */ }
}

const session = new Session(agent, { storage: new TableStorage() });
await session.ready;    // needed before reading session.history when the store is asynchronous
```

Each operation may return its value or a promise. The conditional append is
what keeps two writers from corrupting a session: a tool's intent is appended
before the tool runs, so a writer that has been overtaken gets `FencedError`
(`session_fenced`) and never runs the tool. Any other storage error means the
outcome is unknown, so the `Session` stops accepting work; open it again.

The package exports the contract as a check you can run against your store:

```typescript
import test from "node:test";
import { checkStorage } from "tiny-agent-sdk";

test("TableStorage keeps the storage contract", () => checkStorage(new TableStorage()));
```

`checkStorage` throws an `AssertionError` at the first rule the store breaks.
It creates two small sessions and leaves them, because a store never deletes.

Journals are not portable between the language implementations of TinyAgent.

### Stream and cancel

`session.stream` yields ordered events. Each carries a `runId` and a `sequence`
that starts at one.

```typescript
const controller = new AbortController();
try {
  for await (const event of session.stream("And for Porto?", { signal: controller.signal })) {
    if (event.type === "text_delta") process.stdout.write(event.delta);
    if (event.type === "tool_execution_start") controller.abort();
  }
} catch (error) {
  if (!controller.signal.aborted) throw error;
}
```

The event types are `agent_start`, `text_delta`, `tool_execution_start`,
`tool_execution_end`, `compaction_start`, `compaction_end`, and `agent_end`.
`agent_end.status` is `completed`, `failed`, `aborted`, or `paused`, and it
carries the run's `usage`. `tool_execution_start` carries the call's
`executionId`. After a failed or aborted run the iterator throws, and nothing
from that run joins the history. `session.run(prompt, { signal, onEvent })` is
the same loop with a callback.

A session runs one thing at a time. A second `run`, `stream`, or `compact`
while one is active throws `SessionBusyError`. `close()` cancels whatever is
running, including a `stream` loop you walked away from, and waits for its
cleanup.

### Sending a prompt once

Give a prompt a `messageId` of your own and sending it twice is safe:

```typescript
const first = await session.run("Charge the order.", { messageId: "order-1841" });
const again = await session.run("Charge the order.", { messageId: "order-1841" });
again.repeated;   // true: the stored answer, with no model or tool call
```

If that run committed, you get its stored result with `repeated: true`. If it
was interrupted, it is resumed. If it failed, it runs again. If it is running
now, `SessionBusyError`.

### Events from the journal

```typescript
const { events, nextAfter } = session.events({ after: 0, limit: 100 });
```

`session.events` rebuilds events from the journal for every run: `agent_start`,
one `text_delta` per stored assistant message, `tool_execution_start`,
`tool_execution_end`, and `agent_end`. A run that failed or was cancelled is
included, with its real status and error, so a client that polls can tell. Each
event has a `cursor`, the journal sequence of the record it came from; pass the
last one you saw as `after` to get only what is newer. `nextAfter` is `null` on
the last page. The live deltas of a run are not stored, so a replay has the
text in one piece. `session.cursor` is the sequence of the newest record.

Live events from a session carry the same `cursor` when a journal record stands
behind them (`agent_start`, the tool events, `agent_end`, and a compaction that
wrote a checkpoint), so a client can switch between a live stream and a replay
without seeing anything twice. Text and reasoning deltas have none.

### Restart and resume

```typescript
Session.list(".tinyagent/sessions");                              // ["s_3f0c..."]
const session = Session.open(agent, ".tinyagent/sessions", "s_3f0c...");
console.log(session.history.length);
await session.run("Which of the two is warmer?");
```

Each session is one append-only file, `<id>.jsonl`, where every record is
versioned and checksummed. What you can rely on:

- **One writer.** Opening a session that another object or process holds
  throws `SessionBusyError`. The lock belongs to the operating system, so a
  crashed process releases it. The lock fails fast; the conditional append
  described under Storage is what protects the journal.
- **A changed agent.** A session may be reopened with other instructions or
  tools. The change is written to the journal as a `configuration` record.
- **Torn writes.** If a crash cuts the last record short, that fragment is
  dropped on open and the rest loads.
- **Corruption.** A complete record that fails its checksum, order, or
  encoding makes `open` throw `SessionCorruptionError`. The file is left as it
  was found.
- **Failed writes.** If the store refuses a record, or answers with the wrong
  sequence, the run ends with `error.code: "storage_error"` (a `StorageError`;
  `session_fenced` when another writer got there first), no further tool
  starts, and the session object refuses further work with
  `SessionFailedError` (code `session_failed`). Open the session again. Over
  HTTP such a session answers 409 `session_failed`.
- **Failed and cancelled runs.** A tool call is written down before the tool
  runs (a `tool_intent` record) and again when it finishes (`tool_result`). If
  a run fails or is cancelled while a tool is still running, the call is
  recorded as interrupted and the run as failed or aborted. Nothing of that
  run joins the conversation, and the next prompt is accepted.

`session.history` is the committed conversation. `session.records` is
everything in the journal, including runs that failed.

### Interrupted runs

If the process dies in the middle of a run, the journal holds a run that was
started and neither committed nor failed. After reopening, `session.resumable`
is true and `session.resume()` continues that run in the same agent loop:

```typescript
const session = Session.open(agent, ".tinyagent/sessions", "s_3f0c...");
if (session.resumable) await session.resume({ onEvent: show });
```

- A tool call that had finished keeps its result and is never run again.
- A tool call that had not started yet runs now.
- A call that was in flight, whose tool is `idempotent` both in the journal
  and in today's agent, runs again with the same `executionId`. Its
  `tool_execution_start` event has `rerun: true`.
- Any other call in flight is not run again. The model gets this error as the
  tool's result and the run goes on: "This tool call was interrupted and may
  have partly run. Check its effects before continuing." Its
  `tool_execution_start` event has `interrupted: true`.
- If the final answer was already journaled, the run is committed without a
  model request.

The resumed run's `agent_start` has `resumed: true`. While a run is waiting to
be resumed, a new prompt throws `ResumeError` with code `resume_required`;
`resume()` with nothing to resume throws `nothing_to_resume`. Limits and
`usage` count what the resumed part did. The `model`, `instructions`, `extra`,
`outputSchema` and limits given to the original run are stored with it and
used again by `resume()`; `context` is never stored, so pass it again.

To decide yourself instead of letting the model sort it out, create the
session with `onUncertain: "reconcile"`. Then an interrupted call that is not
safe to repeat sets `recoveryRequired`, and the session waits for
`reconcile(note)`. It sets the interrupted run aside and adds your note to
the conversation, so the model knows what happened. This is the only case in
which `recoveryRequired` is true.

`session.abandon(reason)` ends an interrupted or paused run without
continuing it. The run is recorded as failed with code `abandoned`, and the
session takes a new prompt. With no such run it throws `nothing_to_resume`.

### Pausing for an answer

A tool handler, or `beforeTool`, may return `pause(payload)`. The run then
ends with `agent_end` `status: "paused"` and `result.pause`
(`{ toolCallId, toolName, payload }`), and stays open in the journal. Nothing
is uncertain about it. Later, in this process or another, supply the tool
call's result:

```typescript
import { pause } from "tiny-agent-sdk";

const approve = new Tool({
  name: "deploy", description: "Deploy to production.", parameters: { type: "object" },
  handler: () => pause({ question: "Deploy to production?" }),
});

const result = await session.run("Ship it.");
if (result.pause) {                              // also session.paused, after a reopen
  await session.resume({ result: "approved by Ana" });      // or { error: "Refused." }
}
```

This one mechanism covers human approval, asking the user a question, tools
that the client executes, and waiting for a webhook. On resume neither the
handler nor `beforeTool` is called again for that call: the value you pass is
the tool's result (`afterTool` still sees it). `error` must be a string;
`result` may be any JSON value, `null` included, and is held to the same size
limit as any tool output. A wrong argument is refused and the pause stays.

In the event stream a call that pauses gets a `tool_execution_end` with
`paused: true` and empty output before the paused `agent_end`. When it is
answered, the resumed run emits a `tool_execution_start` with `resumed: true`
and an end carrying the result. `session.events()` replays the same.

### Compact

A long conversation outgrows the model's context window. Give the session a
budget and it summarizes older turns before a request would exceed it. You
supply the token counter.

```typescript
const session = new Session(agent, {
  directory: ".tinyagent/sessions",
  budget: {
    maxTokens: 200_000,        // the model's context window
    responseReserve: 8_000,    // room kept free for the answer
    countTokens: (request) => Math.ceil(JSON.stringify(request).length / 4),
    keepTurns: 2,              // recent exchanges that are never summarized
  },
});

await session.compact();       // or let it happen automatically during run()
```

`countTokens` receives the whole request: model, instructions, history, and
tool definitions. Compaction is an ordinary model call with no tools. It never
separates a tool call from its result, and each later compaction updates the
previous summary. The summary is checked and written to disk before it is
used. If the model call fails, is cancelled, or returns a summary that does
not help, an error is thrown and the context stays as it was. Compaction
changes what the model is sent. `session.history` and the file keep every
message. Pass the same budget to `Session.open` to keep compacting after a
resume.

## The complete example

[`examples/session.ts`](examples/session.ts) puts all of this in one file. Run
it twice:

```sh
export OPENAI_API_KEY=...
node examples/session.ts   # answers, cancels a second run at its tool call, exits
node examples/session.ts   # reopens the session, continues, compacts, continues
```

`example.test.ts` runs that file as two real processes against a local stand-in
for the OpenAI API, so `npm test` exercises it without a key or a network.

## The coding CLI

The package installs a `tinyagent` command: a coding agent with one guarded
Bash tool, working in the current directory.

```sh
npx tinyagent                        # interactive; prints its session ID
npx tinyagent --list-sessions
npx tinyagent --resume s_3f0c...
npx tinyagent -p "run the tests" --yolo     # one run, then exit 0 if it completed, 1 if not
npx tinyagent --mode rpc             # JSONL commands on stdin, events on stdout
npx tinyagent serve                  # HTTP on 127.0.0.1:8080
```

At the prompt, `/history` prints the journal, `/compact` compacts,
`/resume` continues an interrupted run, `/resume <text>` answers a paused one
with that text, `/abandon` drops one, and `/reconcile <note>` settles an
interrupted tool. With `--resume`, a run that
was interrupted is continued first, in interactive and one-shot mode alike.

Before each command the interactive CLI shows it and asks. Answer `y` or `yes` to
run it, `n` or `no` to reject it, `a` or `all` to run it and stop asking for
the rest of the session (in any case), or type a reason: the command is rejected and the model is told why.
`--yolo` (or `TINYAGENT_YOLO=1`) turns the question off. `--allow <regex>`,
which may be repeated, runs a command without asking when the pattern matches
the whole command. A command that chains, substitutes or redirects (it contains
any of `; & | $ < > ( )`, a backtick, a backslash or a newline) is never
auto-approved, so `--allow 'echo .*'` does not approve `echo hi; curl x | sh`. One-shot mode
(`-p` or `--prompt`) cannot ask, so without `--yolo` or a matching `--allow`
it rejects every command and tells the model so. RPC and HTTP modes never
ask. The blacklist below applies in every mode.

| Flag | Meaning |
| --- | --- |
| `-p`, `--prompt <task>` | Run one task, stream the answer to stdout, and exit |
| `--max-steps`, `--max-seconds`, `--max-tokens` | Limits for each run |
| `--cwd <directory>` | Where Bash commands run |
| `--yolo`, `--allow <regex>` | Approval, as above |
| `--resume <id>`, `--list-sessions` | Saved sessions |

| Variable | Meaning |
| --- | --- |
| `TINYAGENT_PROVIDER` | `openai` (default), `anthropic`, or `chat` for an OpenAI-compatible Chat Completions server. `chat` requires `TINYAGENT_MODEL`; its key is optional and read from `OPENAI_API_KEY` when set |
| `TINYAGENT_BASE_URL` | The provider's API root, for a gateway or a local server |
| `TINYAGENT_MODEL` | Model name |
| `TINYAGENT_SESSION_DIR` | Where sessions are saved. Default `.tinyagent/sessions` |
| `TINYAGENT_CONTEXT_TOKENS` | Turns on compaction with this context size |
| `TINYAGENT_RESPONSE_RESERVE` | Room kept for the answer. Default 4096 |
| `TINYAGENT_MAX_STEPS`, `TINYAGENT_MAX_SECONDS`, `TINYAGENT_MAX_TOKENS` | The limits, when the flag is absent |
| `TINYAGENT_YOLO` | `1` runs commands without asking |
| `TINYAGENT_HOST`, `TINYAGENT_PORT`, `TINYAGENT_TOKEN`, `TINYAGENT_ALLOW_REMOTE` | `serve` options |
| `TINYAGENT_ALLOWED_HOSTS`, `TINYAGENT_ALLOWED_ORIGINS` | Extra host names and browser origins `serve` accepts, comma separated |

Only the CLI reads these variables. The library never does.

The Bash tool refuses a short list of destructive commands. That list is a
guard against accidents. It is not a sandbox. A command is stopped after 120
seconds.

To give your own agent the same tool:

```typescript
import { Agent, bashTool } from "tiny-agent-sdk";

const bash = bashTool({
  cwd: "/srv/checkout",
  env: { CI: "1" },                      // merged over the process environment
  timeoutSeconds: 60,
  approve: (command) => /^git (status|diff)/.test(command) || "Only read-only git commands are allowed.",
});
```

`approve` returns `true` to run the command, or `false` or a reason to reject
it; the model sees "Rejected by user: <reason>". It is a `beforeTool` hook for
this one tool. On a timeout the command's whole process group is killed and
the model gets, as a tool error, the usual shape with `exit_code: 124`, the
output so far, and a last line `[timed out after N s]`. Output over 30,000 bytes per stream is not an error: the first
and last 15,000 bytes are kept around a line `[... N bytes elided ...]`.
`runBash(command, signal, { cwd, env, timeoutSeconds })` runs one command
without the tool around it, and `runCli({ argv, env, provider })` is the CLI
itself as a function that resolves to the exit code.

## RPC and HTTP from your own program

Both adapters take the agent you configured and the same session options.

```typescript
import { runRpc, serve } from "tiny-agent-sdk";

const server = await serve(agent, { port: 8080, token: process.env.TINYAGENT_TOKEN, directory: ".tinyagent/sessions" });
console.log(server.url);
await server.close();

await runRpc(agent, process.stdin, process.stdout, { directory: ".tinyagent/sessions" });
```

The server binds to `127.0.0.1`. Any other host needs `allowRemote: true` and a
token. Deleting an HTTP session closes it and keeps its file.

Because the server can run shell commands, it refuses requests a web page
could trick a browser into sending. A request whose `Host` header is not the
address the server is bound to (or `localhost` or a loopback address when it
is bound to loopback, or a name in `allowedHosts`) gets 421. A request with an
`Origin` header that is not in `allowedOrigins` gets 403; by default no origin
is allowed. Both checks apply to every route, with or without a token. The CLI
reads `TINYAGENT_ALLOWED_HOSTS` and `TINYAGENT_ALLOWED_ORIGINS`, comma
separated.

Over RPC and HTTP the line that ends a run is sent only after the outcome is
in the journal and the session is idle, so the next command is accepted as
soon as a client has read it, and a run whose commit failed is reported as
failed, never as completed.

| | RPC command | HTTP route |
| --- | --- | --- |
| Start a run | `prompt` | `POST /sessions/:id/prompts` |
| Continue an interrupted or paused run | `resume_run` | `POST /sessions/:id/resume` |
| Drop one instead | `abandon_run` | `POST /sessions/:id/abandon` |
| Read events from the journal | `get_events` | `GET /sessions/:id/events?after=0&limit=100` |
| Cancel it | `abort` | `POST /sessions/:id/cancel` |
| New session | `new_session` | `POST /sessions` |
| List saved sessions | `list_sessions` | `GET /sessions` |
| Resume one | `resume_session` | `POST /sessions` with `{"sessionId": "s_..."}` |
| Read history | `get_history` | `GET /sessions/:id?offset=0&limit=20&view=history` |
| Compact | `compact` | `POST /sessions/:id/compact` |
| Reconcile | `reconcile` | `POST /sessions/:id/reconcile` |
| Run state | `get_state` | |
| Close | end of input | `DELETE /sessions/:id` |

`GET /health` reports that the server is up. [PROTOCOL.md](../../PROTOCOL.md)
defines framing, errors, and limits for protocol version 1; the version 2
additions described here are not in that document yet.

`prompt` and `POST .../prompts` accept `messageId`; the RPC response then has
`repeated`. `resume_run` and `POST .../resume` accept `{"result": ...}` or
`{"error": "..."}` for a paused run. `get_state` adds `resumable`, `paused`
and the last run's `usage`; the session page adds `resumable` and `paused`.
`abandon_run` and `POST .../abandon` accept an optional `reason` and answer
`{"abandoned": true}`. A refused option, prompt or resume input is `invalid_command` over RPC and
`invalid_request` (400) over HTTP. New error codes: `resume_required`, `nothing_to_resume` and `session_fenced`
(HTTP 409). Both adapters take `storage` in place of `directory`.

### Runs that outlive their connection

Over HTTP, disconnecting from a prompt cancels its run. Send
`"detach": true` in the body of `prompts` or `resume` and it does not: the run
goes on to its end, and only `POST /sessions/:id/cancel`, deleting the session,
or shutting the server down stops it. The response still streams events for
as long as you stay connected.

```sh
curl -N localhost:8080/sessions/$ID/prompts -H 'Content-Type: application/json' \
  -d '{"message":"Run the migration","detach":true,"messageId":"migration-7"}'
curl -N "localhost:8080/sessions/$ID/events?after=0&follow=1"
```

`GET /sessions/:id/events?after=N&follow=1` streams JSONL: first the events
rebuilt from the journal with a cursor above `N`, then, if a run is active,
the text of the turn in progress and its live events as they happen, ending
after that run's `agent_end`. With no active run it ends after the replay.
Live deltas carry no `cursor`; the other events carry the one a replay would
give them. A follower that cannot keep up is disconnected;
it never slows the run. The server does not resume interrupted runs when it
starts: reopen the session and post to `/resume`. In RPC mode nothing changes:
the end of input cancels the run.

## Do it yourself

Some things are left out on purpose, because they are a few lines on top of
what is here.

**An agent as a tool.** A handler may run another agent:

```typescript
const researcher = new Agent({ model: "gpt-5.6", instructions: "Research the question.", tools: [search] });
const research = new Tool({
  name: "research", description: "Ask the research agent.",
  parameters: { type: "object", properties: { question: { type: "string" } }, required: ["question"] },
  handler: async ({ question }, signal) => (await researcher.run(String(question), [], { signal })).text,
});
```

**MCP tools.** List the server's tools with an MCP client of your choice and
wrap each one. `client` here is the official SDK's `Client`:

```typescript
const { tools } = await client.listTools();
const mcpTools = tools.map((remote) => new Tool({
  name: remote.name, description: remote.description ?? "", parameters: remote.inputSchema,
  handler: (args) => client.callTool({ name: remote.name, arguments: args }),
}));
```

**OpenTelemetry spans.** Events carry everything a span needs:

```typescript
const spans = new Map<string, Span>();
await session.run(prompt, { onEvent(event) {
  if (event.type === "tool_execution_start")
    spans.set(event.toolCallId, tracer.startSpan(`tool ${event.toolName}`, { attributes: { "tool.execution_id": event.executionId } }));
  if (event.type === "tool_execution_end") spans.get(event.toolCallId)?.end();
} });
```

**A cost estimate.** Multiply `usage` by your model's prices:

```typescript
const { inputTokens, outputTokens } = result.usage;
const dollars = (inputTokens * 1.25 + outputTokens * 10) / 1_000_000;   // your prices per million tokens
```

## Good to know

- Sessions lock through `node:sqlite`. Node 22 prints one `ExperimentalWarning`
  about it on stderr the first time a process opens a durable session. Node 24
  and later print nothing. In-memory sessions never load it.
- CI runs the tests on Linux and macOS with Node.js 22 and 24. Windows is
  untested.
- A tool whose effects matter should be `idempotent` and use `executionId` as
  its idempotency key, or should record enough for you or the model to check
  after an interruption. TinyAgent does not promise exactly-once effects.
- Not included: parallel tool calls, an MCP client, orchestration, memory or
  retrieval, and dollar costs.

## Working on the SDK

```sh
npm ci --ignore-scripts
npm run check    # type-check the library, tests, and example
npm test         # offline; needs no API key
npm run build    # compile to dist/
```

The sources run directly under Node, without a build step.

| File | What it holds |
| --- | --- |
| `sdk.ts` | The library: `Agent`, its loop, the public exports, and `runCli` |
| `tiny_agent.ts` | The command-line entry file. Nothing else asks whether it is the program |
| `session.ts` | `Session`: history, resume, recovery, events, and compaction |
| `storage.ts` | `Storage`, `FileStorage`, `MemoryStorage`, and `checkStorage` |
| `bash.ts` | The guarded Bash tool |
| `scripted.ts` | `ScriptedProvider`, the fake model for tests |
| `types.ts` | `Tool`, events, and shared types |
| `openai.ts`, `anthropic.ts`, `chat.ts`, `sse.ts` | The three providers and their streaming |
| `rpc.ts`, `http.ts`, `protocol.ts` | The adapters and their limits |

MIT licensed. See [LICENSE](LICENSE).
