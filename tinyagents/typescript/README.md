# TinyAgent for TypeScript

A small agent SDK you can read in an afternoon. It calls OpenAI Responses and
Anthropic Messages directly, runs your tools, streams what happens, and keeps a
conversation on disk so that it survives a restart.

There are three things to learn:

- **Tool**: a function the model may call, with a JSON Schema for its arguments.
- **Agent**: a model, instructions, and tools. It runs one prompt to completion.
- **Session**: a conversation. It remembers history, saves it, resumes it, and compacts it.

A coding CLI, a JSONL RPC mode, and an HTTP server are included. All three are
thin adapters over the same `Agent` and `Session`.

## Install

TinyAgent needs Node.js 22.18 or later. It is not on the npm registry yet, so
build the package from a clone and install the tarball:

```sh
git clone https://github.com/superuserkalo/tinyagent.git
cd tinyagent/tinyagents/typescript
npm ci --ignore-scripts
npm pack                      # builds dist/ and writes tiny-agent-sdk-0.0.1.tgz

cd /path/to/your/project      # a project with "type": "module"
npm install /path/to/tinyagent/tinyagents/typescript/tiny-agent-sdk-0.0.1.tgz
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
the handler runs. Invalid arguments and thrown errors go back to the model as a
tool error, so it can correct itself. Handlers may be async. They receive the
run's `AbortSignal` and should stop when it aborts. Tools run one at a time.

To use Anthropic, pass its provider:

```typescript
import { Agent, AnthropicProvider } from "tiny-agent-sdk";

const agent = new Agent({ model: "claude-sonnet-4-6", provider: new AnthropicProvider() });
```

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
`agent_end.status` is `completed`, `failed`, or `aborted`. After a failed or
aborted run the iterator throws, and nothing from that run joins the history.
`session.run(prompt, { signal, onEvent })` is the same loop with a callback.

A session runs one thing at a time. A second `run`, `stream`, or `compact`
while one is active throws `SessionBusyError`. Finish or leave a `stream` loop
before you call `close()`.

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
  crashed process releases it.
- **Same agent.** A session reopens only with the model, instructions, and
  tools it was created with.
- **Torn writes.** If a crash cuts the last record short, that fragment is
  dropped on open and the rest loads.
- **Corruption.** A complete record that fails its checksum, order, or
  encoding makes `open` throw `SessionCorruptionError`. The file is left as it
  was found.
- **Failed writes.** If the disk refuses a record, the run stops before any
  tool executes and the session object refuses further work. Reopen it.
- **Interrupted tools.** A tool call is written down before the tool runs. If
  the run then ends without committing (a crash, a cancellation, a failed
  consumer), the session cannot know whether the tool's effect happened, and
  it will not run the tool again by itself. `session.recoveryRequired` is
  true and `run` throws `RecoveryRequiredError` until you look and decide:

```typescript
if (session.recoveryRequired) {
  console.log(session.records.filter((record) => record.type === "tool_intent").at(-1));
  session.reconcile("The Porto lookup was cancelled. It changes nothing; safe to ask again.");
}
```

Your note becomes part of the conversation, so the model knows what happened.
`session.history` is the committed conversation. `session.records` is
everything in the journal, including runs that failed.

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
node examples/session.ts   # reopens the session, reconciles, continues, compacts, continues
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
npx tinyagent --mode rpc             # JSONL commands on stdin, events on stdout
npx tinyagent serve                  # HTTP on 127.0.0.1:8080
```

At the prompt, `/history` prints the journal, `/compact` compacts, and
`/reconcile <note>` settles an interrupted tool.

| Variable | Meaning |
| --- | --- |
| `TINYAGENT_PROVIDER` | `openai` (default) or `anthropic` |
| `TINYAGENT_MODEL` | Model name |
| `TINYAGENT_SESSION_DIR` | Where sessions are saved. Default `.tinyagent/sessions` |
| `TINYAGENT_CONTEXT_TOKENS` | Turns on compaction with this context size |
| `TINYAGENT_RESPONSE_RESERVE` | Room kept for the answer. Default 4096 |
| `TINYAGENT_HOST`, `TINYAGENT_PORT`, `TINYAGENT_TOKEN`, `TINYAGENT_ALLOW_REMOTE` | `serve` options |

The Bash tool refuses a short list of destructive commands. That list is a
guard against accidents. It is not a sandbox.

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

| | RPC command | HTTP route |
| --- | --- | --- |
| Start a run | `prompt` | `POST /sessions/:id/prompts` |
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
defines framing, errors, and limits.

## Good to know

- Sessions lock through `node:sqlite`. Node 22 prints one `ExperimentalWarning`
  about it on stderr the first time a process opens a durable session. Node 24
  and later print nothing. In-memory sessions never load it.
- Tested on macOS. CI is set up for macOS and Linux. Windows is untested.
- A tool whose effects matter should be safe to repeat, or should record enough
  for you to reconcile after an interruption. TinyAgent does not promise
  exactly-once effects.

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
| `tiny_agent.ts` | `Agent`, its loop, the public exports, and the CLI |
| `session.ts` | `Session`: history, resume, recovery, and compaction |
| `storage.ts` | The journal file and its writer lock |
| `types.ts` | `Tool`, events, and shared types |
| `openai.ts`, `anthropic.ts`, `sse.ts` | The two providers and their streaming |
| `rpc.ts`, `http.ts`, `protocol.ts` | The adapters and their limits |

MIT licensed. See [LICENSE](LICENSE).
