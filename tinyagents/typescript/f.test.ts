/** Protocol version 2, Parts E and F: scenarios 32, 33 and 36 to 39. */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:http";
import { PassThrough } from "node:stream";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { HttpFixture, body, json, object } from "./http_fixture.ts";
import { resumeAgent } from "./resume_fixture.ts";
import { Agent, AnthropicProvider, ChatCompletionsProvider, MemoryStorage, OpenAIProvider, ProviderError, RpcSession,
  ScriptedProvider, Session, Tool, ToolError, bashTool, pause, runBash, runCli, type AgentEvent, type ContentPart, type Item, type Provider,
  type ScriptedTurn } from "./tiny_agent.ts";

function call(name: string, arguments_: Item = {}, callId = `call_${name}`): ScriptedTurn {
  return [{ callId, name, arguments: arguments_ }];
}
function sse(...events: Item[]): string {
  return events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("");
}
const types = (events: AgentEvent[]) => events.map((event) => event.type);

const photo: ContentPart = { type: "image", mediaType: "image/png", data: "iVBORw0KGgo=" };
const linked: ContentPart = { type: "image", url: "https://images.example/chart.png" };
const camera = new Tool({ name: "camera", description: "Take a picture.", parameters: { type: "object" },
  handler: (): ContentPart[] => [{ type: "text", text: "The front door." }, photo] });

test("D32 images in a prompt and in a tool result reach each provider in its native shape", async (t) => {
  const prompt: ContentPart[] = [{ type: "text", text: "What is in these?" }, photo, linked];
  const sent: Item[] = [];
  const fetch = (...responses: string[]) => async (_input: string | URL | Request, init?: RequestInit) => {
    sent.push(JSON.parse(String(init?.body)) as Item);
    return new Response(responses.shift());
  };
  const dataUrl = "data:image/png;base64,iVBORw0KGgo=";

  // OpenAI Responses.
  const openai = new OpenAIProvider({ apiKey: "k", fetch: fetch(
    sse({ type: "response.completed", response: { output: [{ type: "function_call", call_id: "c1", name: "camera", arguments: "{}" }] } }),
    sse({ type: "response.completed", response: { output: [{ type: "message", content: [{ type: "output_text", text: "A door." }] }] } })) });
  const events: AgentEvent[] = [];
  const session = new Session(new Agent({ model: "m", provider: openai, tools: [camera] }), { storage: new MemoryStorage() });
  t.after(() => session.close());
  const result = await session.run(prompt, { onEvent: (event) => { events.push(event); } });
  assert.equal(result.text, "A door.");
  assert.deepEqual((sent[0].input as Item[])[0], { role: "user", content: [{ type: "input_text", text: "What is in these?" },
    { type: "input_image", image_url: dataUrl }, { type: "input_image", image_url: linked.url }] });
  assert.deepEqual((sent[1].input as Item[]).at(-1), { type: "function_call_output", call_id: "c1",
    output: [{ type: "input_text", text: "The front door." }, { type: "input_image", image_url: dataUrl }] });
  // History, the journal and the event all carry the parts as they were given.
  const parts = [{ type: "text", text: "The front door." }, photo];
  assert.deepEqual(session.history[0], { role: "user", content: prompt });
  assert.deepEqual(session.history[2].output, parts);
  assert.deepEqual(session.records.find((record) => record.type === "run_start")?.item, { role: "user", content: prompt });
  assert.deepEqual((session.records.find((record) => record.type === "tool_result")?.item as Item).output, parts);
  const end = events.find((event) => event.type === "tool_execution_end");
  assert.deepEqual(end?.type === "tool_execution_end" && end.output, parts);
  const history = session.history;

  // Anthropic Messages, fed the same history.
  const stop = [{ type: "message_start", message: { id: "m" } }, { type: "message_delta", delta: { stop_reason: "end_turn" } }, { type: "message_stop" }];
  await new Agent({ model: "m", provider: new AnthropicProvider({ apiKey: "k", fetch: fetch(sse(...stop)) }) }).run([linked], history);
  const messages = sent[2].messages as Item[];
  assert.deepEqual(messages[0], { role: "user", content: [{ type: "text", text: "What is in these?" },
    { type: "image", source: { type: "base64", media_type: "image/png", data: "iVBORw0KGgo=" } },
    { type: "image", source: { type: "url", url: linked.url } }] });
  assert.deepEqual(((messages[2].content as Item[])[0]).content, [{ type: "text", text: "The front door." },
    { type: "image", source: { type: "base64", media_type: "image/png", data: "iVBORw0KGgo=" } }]);
  assert.deepEqual(messages.at(-1), { role: "user", content: [{ type: "image", source: { type: "url", url: linked.url } }] });

  // Chat Completions: a tool message is text only, so the tool's image follows as a user message.
  const chat = new ChatCompletionsProvider({ apiKey: "k", fetch: fetch(sse({ choices: [{ delta: { content: "ok" }, finish_reason: "stop" }] })) });
  await new Agent({ model: "m", provider: chat }).run("And now?", history.slice(0, 3));
  const chatMessages = sent[3].messages as Item[];
  assert.deepEqual(chatMessages[0], { role: "user", content: [{ type: "text", text: "What is in these?" },
    { type: "image_url", image_url: { url: dataUrl } }, { type: "image_url", image_url: { url: linked.url } }] });
  assert.deepEqual(chatMessages.slice(2), [{ role: "tool", tool_call_id: "c1", content: "The front door." },
    { role: "user", content: [{ type: "image_url", image_url: { url: dataUrl } }] }, { role: "user", content: "And now?" }]);

  // Over the wire a prompt may be the same list of parts.
  const provider = new ScriptedProvider(["seen", "seen"]);
  const fixture = new HttpFixture(new Agent({ model: "m", provider }));
  t.after(() => fixture.dispose());
  const id = await fixture.session();
  await body(await fixture.send("POST", `/sessions/${id}/prompts`, JSON.stringify({ message: [linked] }), { "Content-Type": "application/json" }));
  assert.deepEqual(provider.requests[0].history[0].content, [linked]);
  const bad = await fixture.send("POST", `/sessions/${id}/prompts`, JSON.stringify({ message: [{ type: "video" }] }), { "Content-Type": "application/json" });
  assert.equal(bad.statusCode, 400);
  const emitted: Item[] = [];
  const rpc = new RpcSession(new Agent({ model: "m", provider }), (value) => { emitted.push(value as Item); });
  await rpc.handle({ id: "p", type: "prompt", message: [{ type: "text", text: "hi" }, linked] });
  await rpc.waitIdle();
  assert.equal(emitted.at(-1)?.status, "completed");
  await rpc.close();
});

test("D33 trace adds model_request_start and model_request_end with usage and the retry flag, and reasoning_delta", async () => {
  const thinking: Provider = { async *stream() {
    yield { type: "reasoning_delta", delta: "Considering." };
    yield { type: "text_delta", delta: "Answer." };
    return { text: "Answer.", assistant: { role: "assistant", content: [{ type: "text", text: "Answer." }] }, output: [], toolCalls: [],
      usage: { inputTokens: 9, outputTokens: 4 } };
  } };
  let attempts = 0;
  const flaky: Provider = { async *stream(request) {
    if (++attempts === 1) throw new ProviderError("overloaded", { retryable: true });
    return yield* thinking.stream(request);
  } };
  const events: AgentEvent[] = [];
  await new Agent({ model: "m", provider: flaky, trace: true, sleep: async () => {} }).run("go", [], { onEvent: (event) => { events.push(event); } });
  assert.deepEqual(types(events), ["agent_start", "model_request_start", "model_request_end", "model_request_start",
    "reasoning_delta", "text_delta", "model_request_end", "agent_end"]);
  assert.deepEqual(events.slice(1, 4).map(({ runId: _runId, sequence: _sequence, ...rest }) => rest), [
    { type: "model_request_start", attempt: 1 },
    { type: "model_request_end", willRetry: true, error: { code: "provider_error", message: "overloaded" } },
    { type: "model_request_start", attempt: 2 }]);
  assert.deepEqual(JSON.parse(JSON.stringify(events[6])).usage, { inputTokens: 9, outputTokens: 4 });
  assert.equal(events[4].type === "reasoning_delta" && events[4].delta, "Considering.");
  assert.deepEqual(events.map((event) => event.sequence), events.map((_, index) => index + 1));

  // The providers stream reasoning where their API does.
  const anthropic = new AnthropicProvider({ eventTransport: async function* () {
    yield { type: "message_start", message: { id: "m" } };
    yield { type: "content_block_start", index: 0, content_block: { type: "thinking", thinking: "" } };
    yield { type: "content_block_delta", index: 0, delta: { type: "thinking_delta", thinking: "Hmm." } };
    yield { type: "content_block_stop", index: 0 };
    yield { type: "message_delta", delta: { stop_reason: "end_turn" } };
    yield { type: "message_stop" };
  } });
  const openai = new OpenAIProvider({ eventTransport: async function* () {
    yield { type: "response.reasoning_summary_text.delta", delta: "Hmm." };
    yield { type: "response.completed", response: { output: [] } };
  } });
  const chat = new ChatCompletionsProvider({ eventTransport: async function* () {
    yield { choices: [{ delta: { reasoning_content: "Hmm." } }] };
    yield { choices: [{ delta: {}, finish_reason: "stop" }] };
  } });
  for (const provider of [anthropic, openai, chat]) {
    const traced: AgentEvent[] = [];
    await new Agent({ model: "m", provider }).run("go", [], { trace: true, onEvent: (event) => { traced.push(event); } });
    assert.deepEqual(types(traced), ["agent_start", "model_request_start", "reasoning_delta", "model_request_end", "agent_end"]);
  }
});

test("D39 trace off keeps the version 1 event sequence; trace on ends every request, also when cancelled or failed", async () => {
  const script = (): ScriptedTurn[] => [{ text: "Looking.", toolCalls: [{ callId: "c", name: "camera", arguments: {} }] }, "Done."];
  const run = async (agent: Agent, options: { trace?: boolean } = {}) => {
    const events: AgentEvent[] = [];
    await agent.run("go", [], { ...options, onEvent: (event) => { events.push(event); } }).catch(() => {});
    return events;
  };
  const v1 = ["agent_start", "text_delta", "tool_execution_start", "tool_execution_end", "text_delta", "agent_end"];
  const quiet = await run(new Agent({ model: "m", tools: [camera], provider: new ScriptedProvider(script()) }));
  assert.deepEqual(types(quiet), v1);
  // A provider that streams reasoning changes nothing while trace is off.
  const reasoning: Provider = { async *stream() {
    yield { type: "reasoning_delta", delta: "secret thoughts" };
    return { text: "", assistant: { role: "assistant", content: [] }, output: [], toolCalls: [] };
  } };
  assert.deepEqual(types(await run(new Agent({ model: "m", provider: reasoning }))), ["agent_start", "agent_end"]);
  // The agent's setting can be overridden for one run, either way.
  const traced = new Agent({ model: "m", tools: [camera], provider: new ScriptedProvider([...script(), ...script()]), trace: true });
  assert.deepEqual(types(await run(traced, { trace: false })), v1);
  const loud = await run(traced);
  assert.deepEqual(types(loud), ["agent_start", "model_request_start", "text_delta", "model_request_end", "tool_execution_start",
    "tool_execution_end", "model_request_start", "text_delta", "model_request_end", "agent_end"]);

  const paired = (events: AgentEvent[]) => {
    let open = 0;
    for (const event of events) {
      if (event.type === "model_request_start") assert.equal(open++, 0);
      if (event.type === "model_request_end") assert.equal(open--, 1);
    }
    assert.equal(open, 0, "every model_request_start has a model_request_end");
  };
  paired(loud);
  // Cancelled mid-request.
  const controller = new AbortController();
  const waits: Provider = { async *stream(request) {
    yield { type: "text_delta", delta: "partial" };
    const aborted = new Promise((resolve) => request.signal.addEventListener("abort", resolve));
    controller.abort();
    await aborted;
    throw new Error("the provider was stopped");
  } };
  const cancelled: AgentEvent[] = [];
  await assert.rejects(new Agent({ model: "m", provider: waits, trace: true }).run("go", [],
    { signal: controller.signal, onEvent: (event) => { cancelled.push(event); } }), { name: "AbortError" });
  assert.deepEqual(types(cancelled), ["agent_start", "model_request_start", "text_delta", "model_request_end", "agent_end"]);
  paired(cancelled);
  const ended = cancelled[3];
  assert.ok(ended.type === "model_request_end" && "error" in ended);
  assert.deepEqual([ended.error.code, ended.willRetry], ["aborted", false]);
  // Failed for good: four attempts, the last one not retried.
  const failing = await run(new Agent({ model: "m", trace: true, sleep: async () => {},
    provider: { async *stream() { throw new ProviderError("down", { retryable: true }); } } }));
  paired(failing);
  const ends = failing.filter((event) => event.type === "model_request_end");
  assert.deepEqual(ends.map((event) => "willRetry" in event && event.willRetry), [true, true, true, false]);
  assert.equal(JSON.parse(JSON.stringify(failing.at(-1))).status, "failed");
});

/** Leave a session with a run that a crash interrupted mid-tool. */
function crashed(t: { after(hook: () => void): void }, mode = "after_intent", ...flags: string[]) {
  const directory = mkdtempSync(join(tmpdir(), "ta-f-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const log = join(directory, "effects.log");
  writeFileSync(log, "");
  const child = spawnSync(process.execPath, [fileURLToPath(new URL("./resume_fixture.ts", import.meta.url)), directory, "s_crash", log, ...flags],
    { env: { ...process.env, TINYAGENT_TEST_CRASH_AT: mode }, encoding: "utf8" });
  assert.equal(child.status, 41, child.stderr);
  return { directory, log };
}

test("D36 abandon ends an open run and a paused run; a prompt then succeeds; nothing open is nothing_to_resume", async (t) => {
  const { directory, log } = crashed(t);
  for (const onUncertain of ["tell_model", "reconcile"] as const) {
    const { agent, provider } = resumeAgent(log, { turns: 1 });
    const again = crashed(t);
    const session = new Session(agent, { directory: onUncertain === "reconcile" ? again.directory : directory, sessionId: "s_crash",
      resume: true, onUncertain });
    assert.equal(session.recoveryRequired, onUncertain === "reconcile");
    await session.abandon("the operator gave up");
    assert.deepEqual([session.resumable, session.recoveryRequired, session.history.length], [false, false, 0]);
    // The pending call is settled as interrupted and the run is closed as failed with code abandoned.
    assert.deepEqual(session.records.slice(-2).map((record) => [record.type, record.interrupted ?? object(record.error).code]),
      [["tool_result", true], ["run_failed", "abandoned"]]);
    const replay = session.events().events;
    assert.deepEqual(types(replay), ["agent_start", "tool_execution_start", "tool_execution_end", "agent_end"]);
    const end = replay.at(-1);
    assert.ok(end?.type === "agent_end" && end.status === "failed");
    assert.deepEqual(end.error, { code: "abandoned", message: "the operator gave up", retryable: false });
    assert.equal((await session.run("fresh start")).text, "all done");
    assert.equal(provider.requests[0].history.length, 1, "nothing of the abandoned run is in the conversation");
    await assert.rejects(session.abandon(), { code: "nothing_to_resume" });
    await session.close();
  }
  // A paused run, over RPC and HTTP.
  const asking = () => new Agent({ model: "m", tools: [new Tool({ name: "ask", description: "Ask", parameters: { type: "object" },
    handler: () => pause("well?") })], provider: new ScriptedProvider([call("ask"), "moved on"]) });
  const emitted: Item[] = [];
  const rpc = new RpcSession(asking(), (value) => { emitted.push(value as Item); }, () => {});
  await rpc.handle({ id: "none", type: "abandon_run" });
  assert.equal(object(emitted.at(-1)?.error).code, "nothing_to_resume");
  await rpc.handle({ id: "p", type: "prompt", message: "go" });
  await rpc.waitIdle();
  assert.notEqual(rpc.session.paused, undefined);
  await rpc.handle({ id: "a", type: "abandon_run", reason: "nobody answered" });
  assert.deepEqual(emitted.at(-1)?.data, { abandoned: true });
  assert.equal(rpc.session.paused, undefined);
  await rpc.handle({ id: "p2", type: "prompt", message: "again" });
  await rpc.waitIdle();
  assert.deepEqual([emitted.at(-1)?.status, emitted.at(-1)?.text], ["completed", "moved on"]);
  await rpc.close();

  const fixture = new HttpFixture(asking());
  t.after(() => fixture.dispose());
  const id = await fixture.session();
  const nothing = await fixture.send("POST", `/sessions/${id}/abandon`);
  assert.equal(nothing.statusCode, 409);
  assert.equal(object((await json(nothing)).error).code, "nothing_to_resume");
  await body(await fixture.prompt(id, "go"));
  const abandoned = await fixture.send("POST", `/sessions/${id}/abandon`, JSON.stringify({ reason: "timeout" }), { "Content-Type": "application/json" });
  assert.deepEqual([abandoned.statusCode, await json(abandoned)], [200, { abandoned: true }]);
  assert.match(await body(await fixture.prompt(id, "again")), /"status":"completed"/);
});

test("D38 a resumed stream is well-formed: every tool end follows a start for the same call", async (t) => {
  for (const [mode, idempotent, expected] of [["after_intent", false, { interrupted: true }], ["after_intent", true, { rerun: true }],
    ["before_intent", false, {}], ["after_result", false, undefined]] as const) {
    const { directory, log } = crashed(t, mode, ...(idempotent ? ["idempotent"] : []));
    const { agent } = resumeAgent(log, { turns: 1, idempotent });
    const session = new Session(agent, { directory, sessionId: "s_crash", resume: true });
    const events: AgentEvent[] = [];
    await session.resume({ onEvent: (event) => { events.push(event); } });
    const started = new Set<string>();
    for (const event of events) {
      if (event.type === "tool_execution_start") started.add(event.toolCallId);
      if (event.type === "tool_execution_end") assert.ok(started.has(event.toolCallId), `${mode}: an end without a start`);
    }
    const start = events.find((event) => event.type === "tool_execution_start");
    if (!expected) assert.equal(start, undefined, "a finished call is neither started nor ended again");
    else {
      assert.ok(start?.type === "tool_execution_start");
      assert.deepEqual({ ...(start.interrupted ? { interrupted: true } : {}), ...(start.rerun ? { rerun: true } : {}) }, expected);
      // Start and end carry the cursors of the call's records. A rerun is journaled before it happens (G10).
      const end = events.find((event) => event.type === "tool_execution_end");
      const record = (type: string) => session.records.find((saved) => saved.type === type)?.sequence;
      assert.deepEqual([start.cursor, end?.cursor], [record(start.rerun ? "tool_rerun" : "tool_intent"), record("tool_result")]);
    }
    assert.equal(events[0].cursor, 2, "a resumed agent_start points at the run's run_start record");
    assert.equal(events.at(-1)?.cursor, session.records.length);
    await session.close();
  }
});

test("F10 a compaction's summary request counts toward the run that triggered it", async () => {
  const provider: Provider = { async *stream(request) {
    const text = request.instructions.startsWith("Summarize") ? "A short summary." : "answer ".repeat(100);
    return { text, assistant: { role: "assistant", content: text }, output: [], toolCalls: [], usage: { inputTokens: 100, outputTokens: 10 } };
  } };
  const budget = { maxTokens: 2500, responseReserve: 100, countTokens: (request: Item) => JSON.stringify(request).length, keepTurns: 1 };
  const session = new Session(new Agent({ model: "m", provider }), { budget });
  const usages: number[] = [];
  let compacted = false;
  for (let index = 0; index < 5 && !compacted; index++) {
    const events: AgentEvent[] = [];
    const result = await session.run(`prompt ${index}`, { onEvent: (event) => { events.push(event); } });
    compacted = events.some((event) => event.type === "compaction_end");
    usages.push(result.usage.requests);
    if (compacted) {
      assert.deepEqual(result.usage, { inputTokens: 200, outputTokens: 20, requests: 2 });
      assert.equal(events.find((event) => event.type === "compaction_end")?.cursor, session.records.findIndex((record) => record.type === "checkpoint") + 1);
    }
  }
  assert.equal(compacted, true);
  assert.equal(usages[0], 1);
  await session.run("one more");
  if (await session.compact()) assert.deepEqual(session.usage, { inputTokens: 100, outputTokens: 10, requests: 1 });
  await session.close();
});

test("F13 context.timedOut tells a tool's own timeout from a cancelled run", async () => {
  const seen: boolean[] = [];
  const slow = new Tool({ name: "slow", description: "Slow", parameters: { type: "object" }, timeoutSeconds: 0.05,
    handler: (_arguments, signal, context) => new Promise((_resolve, reject) => {
      assert.equal(context.timedOut, false);
      signal.addEventListener("abort", () => { seen.push(context.timedOut); reject(new Error("stopped")); });
    }) });
  const provider = new ScriptedProvider([call("slow"), "done", call("slow")]);
  const agent = new Agent({ model: "m", provider, tools: [slow] });
  await agent.run("go");
  assert.deepEqual(seen, [true]);
  assert.equal(provider.requests[1].history.at(-1)?.output, "timed out after 0.05 s");
  const controller = new AbortController();
  await assert.rejects(agent.run("go", [], { signal: controller.signal, onEvent: (event) => {
    if (event.type === "tool_execution_start") setImmediate(() => controller.abort());
  } }), { name: "AbortError" });
  assert.deepEqual(seen, [true, false]);
});

test("F25 a command the blacklist rejects is a tool error that begins exit_code: 126", async () => {
  await assert.rejects(runBash("sudo true"), (error) => error instanceof ToolError && error.message.startsWith("exit_code: 126\n"));
  const provider = new ScriptedProvider([call("bash", { command: "rm -rf /tmp/never" }), "understood"]);
  const events: AgentEvent[] = [];
  await new Agent({ model: "m", provider, tools: [bashTool()] }).run("go", [], { onEvent: (event) => { events.push(event); } });
  const end = events.find((event) => event.type === "tool_execution_end");
  assert.ok(end?.type === "tool_execution_end");
  assert.deepEqual([end.isError, end.output], [true, "exit_code: 126\nstdout:\n\nstderr:\nblocked: 'rm' is denied\n"]);
  assert.equal(provider.requests[1].history.at(-1)?.isError, true);
});

/** Run the CLI in this process. */
async function cli(argv: string[], env: Record<string, string>, typed = "", provider?: Provider) {
  const stdin = new PassThrough();
  const output = { stdout: "", stderr: "" };
  const sink = (key: "stdout" | "stderr") => {
    const stream = new PassThrough();
    stream.on("data", (chunk: Buffer) => { output[key] += chunk.toString(); });
    return stream;
  };
  stdin.end(typed);
  const code = await runCli({ argv, env, provider, stdin, stdout: sink("stdout"), stderr: sink("stderr") });
  return { code, ...output };
}

test("H10 F15 the CLI takes its provider and base URL from the environment, chat included", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "ta-f-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const seen: { url: string; model: unknown; key: string }[] = [];
  const replies: Record<string, string> = {
    "/v1/chat/completions": sse({ choices: [{ delta: { content: "from chat" }, finish_reason: "stop" }] }) + "data: [DONE]\n\n",
    "/v1/responses": sse({ type: "response.output_text.delta", delta: "from responses" },
      { type: "response.completed", response: { output: [{ type: "message", content: [{ type: "output_text", text: "from responses" }] }] } }),
    "/v1/messages": sse({ type: "message_start", message: { id: "m" } }, { type: "content_block_start", index: 0, content_block: { type: "text", text: "" } },
      { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "from messages" } }, { type: "content_block_stop", index: 0 },
      { type: "message_delta", delta: { stop_reason: "end_turn" } }, { type: "message_stop" }),
  };
  const server = createServer(async (request, response) => {
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(chunk as Buffer);
    seen.push({ url: request.url ?? "", model: (JSON.parse(Buffer.concat(chunks).toString()) as Item).model,
      key: String(request.headers.authorization ?? request.headers["x-api-key"]) });
    response.writeHead(200, { "Content-Type": "text/event-stream" }).end(replies[request.url ?? ""]);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => { server.closeAllConnections(); server.close(resolve); }));
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  const base = { TINYAGENT_SESSION_DIR: directory, TINYAGENT_BASE_URL: `http://127.0.0.1:${address.port}/v1` };
  const chat = await cli(["-p", "hi"], { ...base, TINYAGENT_PROVIDER: "chat", TINYAGENT_MODEL: "local-model" });
  assert.deepEqual([chat.code, chat.stdout], [0, "from chat\n"]);
  const openai = await cli(["-p", "hi"], { ...base, OPENAI_API_KEY: "sk-test" });
  assert.deepEqual([openai.code, openai.stdout], [0, "from responses\n"]);
  const anthropic = await cli(["-p", "hi"], { ...base, TINYAGENT_PROVIDER: "anthropic", ANTHROPIC_API_KEY: "ak-test" });
  assert.deepEqual([anthropic.code, anthropic.stdout], [0, "from messages\n"]);
  assert.deepEqual(seen, [{ url: "/v1/chat/completions", model: "local-model", key: "undefined" },
    { url: "/v1/responses", model: "gpt-5.6", key: "Bearer sk-test" }, { url: "/v1/messages", model: "claude-sonnet-4-6", key: "ak-test" }]);
  await assert.rejects(cli(["-p", "hi"], { ...base, TINYAGENT_PROVIDER: "chat" }), /TINYAGENT_MODEL is not set/);
  await assert.rejects(cli(["-p", "hi"], { ...base, TINYAGENT_PROVIDER: "other" }), /unsupported TINYAGENT_PROVIDER/);
});

test("F15 the CLI continues an interrupted run found by --resume, and /abandon drops one", async (t) => {
  for (const oneShot of [true, false]) {
    const { directory } = crashed(t);
    const provider = new ScriptedProvider(["Picked up where it stopped.", "And the new task is done."]);
    const run = await cli(["--resume", "s_crash", ...(oneShot ? ["-p", "a new task"] : [])], { TINYAGENT_SESSION_DIR: directory },
      oneShot ? "" : "a new task\n", provider);
    assert.equal(run.code, 0, run.stderr);
    // First the interrupted run, whose tool call the model is told about; then the new prompt.
    assert.equal(provider.requests.length, 2);
    assert.match(String(provider.requests[0].history.at(-1)?.output), /interrupted and may have partly run/);
    assert.equal(provider.requests[1].history.at(-1)?.content, "a new task");
    assert.ok(run.stdout.indexOf("Picked up where it stopped.") < run.stdout.indexOf("And the new task is done."));
    const stored = new Session(new Agent({ model: "m", provider }), { directory, sessionId: "s_crash", resume: true });
    assert.deepEqual([stored.resumable, stored.history.length], [false, 6]);
    await stored.close();
  }
  // A paused run cannot be continued from the prompt; /abandon ends it and the session takes a task again.
  const directory = mkdtempSync(join(tmpdir(), "ta-f-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const asking = new Agent({ model: "m", provider: new ScriptedProvider([call("ask")]),
    tools: [new Tool({ name: "ask", description: "Ask", parameters: { type: "object" }, handler: () => pause("well?") })] });
  const paused = new Session(asking, { directory });
  await paused.run("go");
  await paused.close();
  const provider = new ScriptedProvider(["A fresh answer."]);
  const run = await cli(["--resume", paused.id], { TINYAGENT_SESSION_DIR: directory }, "task before\n/abandon\ntask after\n/abandon\n", provider);
  assert.equal(run.code, 0);
  assert.match(run.stdout, /run is paused/);
  assert.match(run.stderr, /error: .*interrupted run/, "a task while the run is still open is refused");
  assert.match(run.stdout, /A fresh answer\./);
  assert.match(run.stderr, /error: session has no interrupted run/, "a second /abandon has nothing to drop");
  const stored = new Session(asking, { directory, sessionId: paused.id, resume: true });
  assert.equal(object(stored.records.find((record) => record.type === "run_failed")?.error).code, "abandoned");
  assert.deepEqual([stored.paused, stored.history.at(-2)?.content], [undefined, "task after"]);
  await stored.close();
});
