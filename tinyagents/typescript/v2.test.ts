/** Protocol version 2, Part D: one test per conformance scenario, titled by its number. */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough } from "node:stream";
import { setTimeout as delay } from "node:timers/promises";
import { test, type TestContext } from "node:test";
import { fileURLToPath } from "node:url";
import { HttpFixture, body, json, object, until } from "./http_fixture.ts";
import { deferred } from "./provider_fixture.ts";
import { resumeAgent } from "./resume_fixture.ts";
import { Agent, FencedError, FileStorage, MemoryStorage, OpenAIProvider, ProviderError, RecoveryRequiredError, ResumeError, RpcSession,
  RunError, ScriptedProvider, Session, Tool, ToolError, bashTool, checkStorage, runBash, runCli,
  type AgentEvent, type Item, type ScriptedTurn, type Storage } from "./tiny_agent.ts";

const here = (name: string) => fileURLToPath(new URL(name, import.meta.url));

function scratch(t: TestContext): string {
  const directory = mkdtempSync(join(tmpdir(), "ta-v2-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  return directory;
}

function call(name: string, arguments_: Item = {}, callId = `call_${name}`): ScriptedTurn {
  return [{ callId, name, arguments: arguments_ }];
}

function echo(options: { idempotent?: boolean } = {}): Tool {
  return new Tool({ name: "echo", description: "Echo", parameters: { type: "object" }, ...options,
    handler: ({ text }) => `echo ${String(text)}` });
}

async function collect(run: (onEvent: (event: AgentEvent) => void) => Promise<unknown>): Promise<AgentEvent[]> {
  const events: AgentEvent[] = [];
  await run((event) => { events.push(event); }).catch(() => {});
  return events;
}

/** Run the resume fixture in a child process until it dies at `crashAt`, then describe what it left. */
function crash(t: TestContext, crashAt: string, ...flags: string[]) {
  const directory = scratch(t);
  const log = join(directory, "effects.log");
  writeFileSync(log, "");
  const child = spawnSync(process.execPath, [here("./resume_fixture.ts"), directory, "s_crash", log, ...flags],
    { env: { ...process.env, TINYAGENT_TEST_CRASH_AT: crashAt }, encoding: "utf8" });
  assert.equal(child.status, 41, child.stderr);
  return { directory, effects: () => readFileSync(log, "utf8").split("\n").filter(Boolean),
    reopen: (options: { idempotent?: boolean; turns?: number; onUncertain?: "tell_model" | "reconcile" } = {}) => {
      const { agent, provider } = resumeAgent(log, options);
      const session = new Session(agent, { directory, sessionId: "s_crash", resume: true, onUncertain: options.onUncertain });
      t.after(() => session.close());
      return { session, provider };
    } };
}

test("D01 the storage suite passes for FileStorage and MemoryStorage", async (t) => {
  await checkStorage(new MemoryStorage());
  const directory = join(scratch(t), "not", "yet", "created");
  await checkStorage(new FileStorage(directory));
  // A second FileStorage, like a second process, finds the same data once the first has let go.
  assert.equal(new FileStorage(directory).list().length, 2);
  // The suite fails a store that breaks the contract: this one accepts a stale append.
  const memory = new MemoryStorage();
  const careless: Storage = { load: (id) => memory.load(id), list: () => memory.list(), release: () => {},
    append: (id, _expected, record) => memory.append(id, memory.load(id)?.length ?? 0, record) };
  await assert.rejects(checkStorage(careless), { name: "AssertionError" });
});

test("D01 a Session works over a store whose operations are asynchronous", async () => {
  const memory = new MemoryStorage();
  const slow: Storage = {
    load: async (id) => { await delay(1); return memory.load(id); },
    append: async (id, expected, record) => { await delay(1); return memory.append(id, expected, record); },
    list: async () => memory.list(),
    release: async () => {},
  };
  await checkStorage(slow);
  const agent = new Agent({ model: "scripted", tools: [echo()],
    provider: new ScriptedProvider([call("echo", { text: "hi" }), "first", "second"]) });
  const session = new Session(agent, { storage: slow });
  await session.run("one");
  await session.close();
  const reopened = new Session(agent, { storage: slow, sessionId: session.id, resume: true });
  await reopened.ready;
  assert.equal(reopened.history.length, 4);
  assert.equal((await reopened.run("two")).text, "second");
  assert.deepEqual(reopened.records.map((record) => record.sequence), reopened.records.map((_, index) => index + 1));
  await reopened.close();
  const missing = new Session(agent, { storage: slow, sessionId: "s_missing", resume: true });
  await assert.rejects(missing.ready, { code: "ENOENT" });
  await assert.rejects(missing.run("x"), { code: "ENOENT" });
});

test("D02 crash before the intent: resume runs the tool once and asks the model again", async (t) => {
  const crashed = crash(t, "before_intent");
  assert.deepEqual(crashed.effects(), []);
  const { session, provider } = crashed.reopen({ turns: 1 });
  assert.equal(session.resumable, true);
  assert.equal(session.recoveryRequired, false);
  const events = await collect((onEvent) => session.resume({ onEvent }));
  assert.deepEqual(events.map((event) => event.type),
    ["agent_start", "tool_execution_start", "tool_execution_end", "text_delta", "agent_end"]);
  assert.equal(events[0].type === "agent_start" && events[0].resumed, true);
  assert.equal(crashed.effects().length, 1);
  // The one request of the resumed process is the model being asked again, now with the tool's result.
  assert.equal(provider.requests.length, 1);
  assert.equal(provider.requests[0].history.at(-1)?.output, "marked x");
  assert.deepEqual(session.history.map((item) => item.role), ["user", "assistant", "tool_result", "assistant"]);
  assert.equal(session.resumable, false);
});

test("D03 crash mid idempotent tool: it is rerun with the same executionId", async (t) => {
  const crashed = crash(t, "after_intent", "idempotent");
  const { session, provider } = crashed.reopen({ idempotent: true, turns: 1 });
  const intent = session.records.find((record) => record.type === "tool_intent");
  assert.equal(intent?.idempotent, true);
  const events = await collect((onEvent) => session.resume({ onEvent }));
  const start = events.find((event) => event.type === "tool_execution_start");
  assert.ok(start?.type === "tool_execution_start");
  assert.equal(start.rerun, true);
  assert.equal(start.executionId, `s_crash:${intent?.sequence}`);
  // Both the crashed process and this one ran the handler, under one execution ID.
  assert.deepEqual(crashed.effects(), [start.executionId, start.executionId]);
  assert.equal(provider.requests.length, 1);
  assert.equal(session.history.length, 4);
  assert.equal(session.records.filter((record) => record.type === "tool_intent").length, 1);
});

test("D04 crash mid non-idempotent tool: it is not rerun and the model is told", async (t) => {
  const crashed = crash(t, "after_intent");
  const { session, provider } = crashed.reopen({ turns: 1 });
  assert.equal(session.recoveryRequired, false);
  assert.equal(session.resumable, true);
  await assert.rejects(session.run("something else"), (error) => error instanceof ResumeError && error.code === "resume_required");
  const events = await collect((onEvent) => session.resume({ onEvent }));
  assert.equal(crashed.effects().length, 1, "only the crashed process ran the tool");
  const end = events.find((event) => event.type === "tool_execution_end");
  assert.ok(end?.type === "tool_execution_end");
  assert.equal(end.isError, true);
  assert.equal(end.output, "This tool call was interrupted and may have partly run. Check its effects before continuing.");
  assert.deepEqual(provider.requests[0].history.at(-1), { role: "tool_result", callId: "call_1", toolName: "effect",
    output: end.output, isError: true });
  assert.equal(JSON.parse(JSON.stringify(events.at(-1))).status, "completed");
  assert.equal(session.history.length, 4);
});

test("D05 the same crash with onUncertain reconcile requires recovery until reconcile", async (t) => {
  const crashed = crash(t, "after_intent");
  const { session, provider } = crashed.reopen({ onUncertain: "reconcile" });
  assert.equal(session.recoveryRequired, true);
  assert.equal(session.resumable, false);
  await assert.rejects(session.resume(), RecoveryRequiredError);
  await assert.rejects(session.run("again"), RecoveryRequiredError);
  await session.reconcile("The mark was made.");
  assert.equal(session.recoveryRequired, false);
  assert.equal(session.resumable, false, "reconciling sets the interrupted run aside, as version 1 did");
  await assert.rejects(session.resume(), { code: "nothing_to_resume" });
  await session.run("again");
  assert.equal(crashed.effects().length, 2, "the new run made its own tool call");
  assert.match(String(provider.requests[0].history[0].content), /Interrupted run recovery: The mark was made\./);
});

test("D06 crash after the result: resume does not rerun the tool", async (t) => {
  const crashed = crash(t, "after_result");
  const { session, provider } = crashed.reopen({ turns: 1 });
  const events = await collect((onEvent) => session.resume({ onEvent }));
  assert.deepEqual(events.map((event) => event.type), ["agent_start", "text_delta", "agent_end"]);
  assert.equal(crashed.effects().length, 1);
  assert.equal(provider.requests.length, 1);
  assert.equal(provider.requests[0].history.at(-1)?.output, "marked x");
  assert.equal(session.history.length, 4);
});

test("D07 crash after the final reply: resume commits with zero provider requests", async (t) => {
  const crashed = crash(t, "after_reply");
  const { session, provider } = crashed.reopen({ turns: 0 });
  assert.deepEqual(session.history, []);
  const events: AgentEvent[] = [];
  const result = await session.resume({ onEvent: (event) => { events.push(event); } });
  assert.equal(result.text, "all done");
  assert.deepEqual(events.map((event) => event.type), ["agent_start", "agent_end"]);
  assert.equal(provider.requests.length, 0);
  assert.equal(crashed.effects().length, 1);
  assert.equal(session.records.at(-1)?.type, "run_commit");
  assert.equal(session.history.length, 4);
});

test("D08 an idempotent flag that differs between record and agent is uncertain", async (t) => {
  for (const [recorded, current] of [[true, false], [false, true]]) {
    const crashed = crash(t, "after_intent", ...(recorded ? ["idempotent"] : []));
    const gated = crashed.reopen({ idempotent: current, onUncertain: "reconcile" });
    assert.equal(gated.session.recoveryRequired, true);
    await gated.session.close();
    const { session } = crashed.reopen({ idempotent: current, turns: 1 });
    const events = await collect((onEvent) => session.resume({ onEvent }));
    const start = events.find((event) => event.type === "tool_execution_start");
    assert.equal(start?.type === "tool_execution_start" && start.rerun, undefined);
    assert.equal(crashed.effects().length, 1);
    assert.match(JSON.stringify(events), /interrupted and may have partly run/);
  }
});

test("D09 a second writer is fenced before its tool executes", async () => {
  const storage = new MemoryStorage();
  let executed = 0;
  const tool = new Tool({ name: "effect", description: "Effect", parameters: { type: "object" },
    handler: () => { executed += 1; return "ok"; } });
  const agent = (turns: ScriptedTurn[]) => new Agent({ model: "scripted", tools: [tool], provider: new ScriptedProvider(turns) });
  const first = new Session(agent([call("effect"), "done"]), { storage });
  const second = new Session(agent([call("effect"), "never"]), { storage, sessionId: first.id, resume: true });
  await first.run("early");
  assert.equal(executed, 1);
  // The second writer's view is stale, so its first append is refused and nothing of its run happens.
  await assert.rejects(second.run("late"), (error) => error instanceof FencedError && error.code === "session_fenced");
  await assert.rejects(second.run("late"), /reopen/, "a fenced session stops accepting work");
  // Overtaken in the middle of a run: the run fails with session_fenced before the tool it was about to call.
  const third = new Session(agent([call("effect"), "never"]), { storage, sessionId: first.id, resume: true });
  const events = await collect((onEvent) => third.run("late", { onEvent: (event) => {
    onEvent(event);
    if (event.type === "agent_start")
      storage.append(first.id, storage.load(first.id)?.length ?? 0, { type: "reconciled", note: "another writer", version: 1 });
  } }));
  const end = events.at(-1);
  assert.ok(end?.type === "agent_end" && end.status === "failed");
  assert.equal(end.error.code, "session_fenced");
  assert.deepEqual(events.map((event) => event.type), ["agent_start", "agent_end"]);
  assert.equal(executed, 1, "only the first writer's tool ran");
  assert.throws(() => new Session(agent([]), { storage, sessionId: first.id }), FencedError, "a duplicate create is fenced");
  // The journal is intact: it reopens with the first writer's conversation.
  const reopened = new Session(agent([]), { storage, sessionId: first.id, resume: true });
  assert.deepEqual(reopened.history.map((item) => item.role), ["user", "assistant", "tool_result", "assistant", "user"]);
  assert.deepEqual(reopened.records.map((record) => record.sequence), reopened.records.map((_, index) => index + 1));
});

test("D10 a repeated messageId after commit returns the stored result without any request", async () => {
  const provider = new ScriptedProvider([{ text: "the answer", usage: { inputTokens: 5, outputTokens: 2 } }]);
  const storage = new MemoryStorage();
  const agent = new Agent({ model: "scripted", provider });
  const session = new Session(agent, { storage });
  const original = await session.run("question", { messageId: "m-1" });
  assert.equal(original.repeated, undefined);
  for (const reader of [session, new Session(agent, { storage, sessionId: session.id, resume: true })]) {
    const events: AgentEvent[] = [];
    const repeated = await reader.run("question", { messageId: "m-1", onEvent: (event) => { events.push(event); } });
    assert.equal(repeated.repeated, true);
    assert.equal(repeated.text, "the answer");
    assert.deepEqual(repeated.usage, { inputTokens: 5, outputTokens: 2, requests: 1 });
    assert.equal(repeated.history.length, 2);
    assert.deepEqual(events.map((event) => event.type), ["agent_start", "agent_end"]);
    const end = events[1];
    assert.ok(end.type === "agent_end" && end.status === "completed" && end.repeated === true);
    assert.equal(end.runId, session.records.find((record) => record.type === "run_start")?.runId);
  }
  assert.equal(provider.requests.length, 1);
  assert.equal(session.records.filter((record) => record.type === "run_start").length, 1);
  // A messageId whose run failed runs again.
  await assert.rejects(session.run("fails", { messageId: "m-2" }), /no turn/);
  await assert.rejects(session.run("fails", { messageId: "m-2" }), /no turn/);
  assert.equal(provider.requests.length, 3);
  // RPC reports the repeat in the prompt's response, before the events.
  const emitted: Item[] = [];
  const rpc = new RpcSession(agent, (value) => { emitted.push(value as Item); }, () => {}, { storage, sessionId: session.id, resume: true });
  await session.close();
  await rpc.handle({ id: "again", type: "prompt", message: "question", messageId: "m-1" });
  await rpc.waitIdle();
  assert.equal(object(emitted[0].data).repeated, true);
  assert.deepEqual(emitted.map((item) => item.type), ["response", "agent_start", "agent_end"]);
  await rpc.handle({ id: "state", type: "get_state" });
  assert.deepEqual(object(emitted.at(-1)?.data).usage, { inputTokens: 5, outputTokens: 2, requests: 1 });
  await rpc.close();
});

test("D11 a repeated messageId on an open run resumes it", async (t) => {
  const crashed = crash(t, "after_result", "", "m-open");
  const { session, provider } = crashed.reopen({ turns: 1 });
  await assert.rejects(session.run("leave a mark", { messageId: "m-other" }), { code: "resume_required" });
  const events: AgentEvent[] = [];
  const result = await session.run("leave a mark", { messageId: "m-open", onEvent: (event) => { events.push(event); } });
  assert.equal(events[0].type === "agent_start" && events[0].resumed, true);
  assert.equal(result.text, "all done");
  assert.equal(result.repeated, undefined);
  assert.equal(crashed.effects().length, 1);
  assert.equal(provider.requests.length, 1);
  assert.equal(session.records.filter((record) => record.type === "run_start").length, 1);
  assert.equal((await session.run("leave a mark", { messageId: "m-open" })).repeated, true);
});

test("D12 D37 events(after) returns exactly the later events with increasing cursors, failed runs included", async (t) => {
  const provider = new ScriptedProvider([{ text: "Looking. ", deltas: ["Loo", "king. "], toolCalls: [{ callId: "c1", name: "echo", arguments: { text: "a" } }] },
    { text: "Found it.", usage: { inputTokens: 3, outputTokens: 4 } }, { error: new Error("down") }, "Second answer."]);
  const session = new Session(new Agent({ model: "scripted", tools: [echo()], provider }));
  t.after(() => session.close());
  await session.run("first");
  await session.run("fails").catch(() => {});
  await session.run("second");
  const { events, nextAfter } = session.events();
  assert.equal(nextAfter, null);
  assert.deepEqual(events.map((event) => event.type), ["agent_start", "text_delta", "tool_execution_start",
    "tool_execution_end", "text_delta", "agent_end", "agent_start", "agent_end", "agent_start", "text_delta", "agent_end"]);
  const cursors = events.map((event) => event.cursor ?? 0);
  assert.ok(cursors.every((cursor, index) => Number.isSafeInteger(cursor) && cursor > (cursors[index - 1] ?? 0)));
  // Each cursor is the sequence of the journal record the event came from.
  const records = session.records;
  assert.deepEqual(cursors.map((cursor) => records[cursor - 1].type), ["run_start", "item", "tool_intent", "tool_result",
    "item", "run_commit", "run_start", "run_failed", "run_start", "item", "run_commit"]);
  assert.equal(events[1].type === "text_delta" && events[1].delta, "Looking. ", "one text_delta per stored assistant text");
  const start = events[2];
  assert.ok(start.type === "tool_execution_start");
  assert.equal(start.executionId, `${session.id}:${start.cursor}`);
  const end = events[5];
  assert.ok(end.type === "agent_end" && end.status === "completed");
  assert.deepEqual([end.text, end.usage], ["Found it.", { inputTokens: 3, outputTokens: 4, requests: 2 }]);
  // The failed run is replayed with its real status and error, though it is not part of the conversation.
  const failed = events[7];
  assert.ok(failed.type === "agent_end" && failed.status === "failed");
  assert.deepEqual([failed.error.code, failed.error.message, failed.runId], ["provider_error", "down", events[6].runId]);
  assert.equal(session.history.some((item) => item.content === "fails"), false);
  for (const [index, cursor] of cursors.entries())
    assert.deepEqual(session.events({ after: cursor }).events, events.slice(index + 1));
  assert.deepEqual(session.events({ after: cursors[3] - 1 }).events[0], events[3]);
  const page = session.events({ after: cursors[0], limit: 3 });
  assert.deepEqual(page, { events: events.slice(1, 4), nextAfter: cursors[3] });
  assert.throws(() => session.events({ limit: 0 }), TypeError);
  // A cancelled run is replayed as aborted.
  const controller = new AbortController();
  await session.run("cancelled", { signal: controller.signal, onEvent: () => controller.abort() }).catch(() => {});
  assert.deepEqual(session.events({ after: cursors.at(-1) }).events.map((event) => [event.type, event.type === "agent_end" && event.status]),
    [["agent_start", false], ["agent_end", "aborted"]]);
  // RPC returns the same object.
  const emitted: Item[] = [];
  const rpc = new RpcSession(session.agent, (value) => { emitted.push(value as Item); });
  await rpc.handle({ id: "e", type: "get_events", after: 0, limit: 100 });
  assert.deepEqual(emitted[0].data, { events: [], nextAfter: null });
  await rpc.close();
});

test("D13 importing and constructing does no I/O, with an empty environment and a read-only directory", (t) => {
  const directory = scratch(t);
  const cwd = join(directory, "readonly");
  const script = join(directory, "construct.mjs");
  writeFileSync(script, `
import fs from "node:fs";
import childProcess from "node:child_process";
import net from "node:net";
import http from "node:http";
import https from "node:https";
import workers from "node:worker_threads";
import { syncBuiltinESMExports } from "node:module";
const touched = [];
// Every later read of the environment is recorded.
const environment = process.env;
process.env = new Proxy(environment, {
  // Node's own module loader looks at NODE_V8_COVERAGE and WATCH_REPORT_DEPENDENCIES for every file it loads.
  get(target, key) { if (typeof key === "string" && !/^(NODE_|WATCH_REPORT_)/.test(key)) touched.push("env " + key); return target[key]; },
  has(target, key) { touched.push("env has " + String(key)); return key in target; },
  ownKeys(target) { touched.push("env keys"); return Reflect.ownKeys(target); },
});
globalThis.fetch = () => { touched.push("fetch"); throw new Error("no network"); };
const record = (module, name) => { for (const [key, value] of Object.entries(module)) if (typeof value === "function" &&
  !/^[A-Z]/.test(key)) module[key] = function (...args) { touched.push(name + "." + key); return value.apply(this, args); }; };
record(childProcess, "child_process"); record(net, "net"); record(http, "http"); record(https, "https");
workers.Worker = class { constructor() { touched.push("worker"); } };
syncBuiltinESMExports();
const sdk = await import(${JSON.stringify(new URL("./sdk.ts", import.meta.url).href)});
const afterImport = [...touched];
// From here on, any use of the filesystem counts too. (Loading modules above had to read them.)
record(fs, "fs"); record(fs.promises, "fs.promises");
syncBuiltinESMExports();
const tool = new sdk.Tool({ name: "t", description: "t", parameters: { type: "object" }, handler: () => "x" });
const bash = sdk.bashTool({ cwd: "/nowhere", timeoutSeconds: 1, approve: () => true });
const agent = new sdk.Agent({ model: "m", tools: [tool, bash] });
new sdk.Agent({ model: "m", provider: new sdk.AnthropicProvider() });
new sdk.Agent({ model: "m", provider: new sdk.OpenAIProvider(), maxSteps: 3 });
const files = new sdk.FileStorage("sessions");
const session = new sdk.Session(agent);
new sdk.Session(agent, { storage: new sdk.MemoryStorage() });
new sdk.ScriptedProvider(["x"]);
new sdk.RpcSession(agent, () => {});
console.log(JSON.stringify({ afterImport, touched, id: session.id, exported: Object.keys(sdk).length }));
`);
  mkdirSync(cwd);
  chmodSync(cwd, 0o555);
  const child = spawnSync(process.execPath, [script], { cwd, env: {}, encoding: "utf8" });
  chmodSync(cwd, 0o755);
  assert.equal(child.status, 0, child.stderr);
  assert.equal(child.stderr, "");
  const report = JSON.parse(child.stdout) as { afterImport: string[]; touched: string[]; id: string; exported: number };
  assert.deepEqual(report.afterImport, [], "importing reads no environment and starts nothing");
  assert.deepEqual(report.touched, [], "constructing reads no environment, files, network, threads or processes");
  assert.match(report.id, /^s_[0-9a-f]{32}$/);
  assert.ok(report.exported > 20);
  assert.equal(existsSync(join(cwd, "sessions")), false);
  assert.deepEqual(spawnSync("ls", ["-A", cwd], { encoding: "utf8" }).stdout, "");
});

test("D14 maxSteps, maxSeconds and maxTokens each end the run with limit_exceeded", async () => {
  const slow = new Tool({ name: "echo", description: "Echo", parameters: { type: "object" },
    handler: async () => { await delay(30); return "ok"; } });
  const usage = { inputTokens: 60, outputTokens: 50 };
  const turns = (): ScriptedTurn[] => [{ toolCalls: [{ callId: "c1", name: "echo", arguments: {} }], usage }, "unreachable"];
  for (const [limit, options] of [["steps", { maxSteps: 1 }], ["seconds", { maxSeconds: 0.01 }], ["tokens", { maxTokens: 100 }]] as const) {
    const provider = new ScriptedProvider(turns());
    // A limit may be set on the agent or for one run; the run's wins.
    const session = new Session(new Agent({ model: "scripted", tools: [slow], provider, maxSteps: 50, maxTokens: 1e9 }));
    const events: AgentEvent[] = [];
    await assert.rejects(session.run("go", { ...options, onEvent: (event) => { events.push(event); } }),
      (error) => error instanceof RunError && error.code === "limit_exceeded" && error.limit === limit);
    const end = events.at(-1);
    assert.ok(end?.type === "agent_end" && end.status === "failed");
    assert.deepEqual({ ...end.error, message: "" }, { code: "limit_exceeded", message: "", retryable: false, limit });
    assert.deepEqual(end.usage, { inputTokens: 60, outputTokens: 50, requests: 1 });
    assert.equal(provider.requests.length, 1, "the limit is checked before the next model request");
    // The completed step is in the journal as part of a failed run, and not in the conversation.
    assert.deepEqual(session.records.slice(-3).map((record) => record.type), ["tool_intent", "tool_result", "run_failed"]);
    assert.deepEqual(session.history, []);
    assert.equal(session.recoveryRequired, false);
    assert.deepEqual(session.usage, end.usage);
    await session.close();
  }
  const agent = new Agent({ model: "scripted", provider: new ScriptedProvider(["never"]), maxSteps: 0 });
  await assert.rejects(agent.run("go"), { code: "limit_exceeded", limit: "steps" });
  const unlimited = await new Agent({ model: "scripted", tools: [slow], provider: new ScriptedProvider(turns()) }).run("go");
  assert.deepEqual(unlimited.usage, { inputTokens: 60, outputTokens: 50, requests: 2 });
});

test("D15 an unknown tool is a recoverable observation; three all-error turns end the run", async () => {
  const provider = new ScriptedProvider([call("nope"), "recovered"]);
  const events: AgentEvent[] = [];
  const result = await new Agent({ model: "scripted", provider }).run("go", [], { onEvent: (event) => { events.push(event); } });
  assert.equal(result.text, "recovered");
  assert.deepEqual(provider.requests[1].history.at(-1),
    { role: "tool_result", callId: "call_nope", toolName: "nope", output: "Unknown tool: nope", isError: true });
  assert.deepEqual(events.map((event) => event.type),
    ["agent_start", "tool_execution_start", "tool_execution_end", "text_delta", "agent_end"]);

  const failing = new Tool({ name: "fail", description: "Fail", parameters: { type: "object" },
    handler: () => { throw new Error("boom"); } });
  const mixed: ScriptedTurn = [{ callId: "a", name: "fail", arguments: {} }, { callId: "b", name: "echo", arguments: { text: "ok" } }];
  // Two failing turns, one turn with a success, then three failing turns in a row.
  const script = new ScriptedProvider([call("fail"), call("nope"), mixed, call("fail"), call("fail"), call("nope"), "unreachable"]);
  const session = new Session(new Agent({ model: "scripted", tools: [failing, echo()], provider: script }));
  const ended = await collect((onEvent) => session.run("go", { onEvent }));
  assert.equal(script.requests.length, 6);
  assert.equal(script.remaining, 1);
  const end = ended.at(-1);
  assert.ok(end?.type === "agent_end" && end.status === "failed");
  assert.equal(end.error.code, "repeated_tool_errors");
  assert.equal(end.error.retryable, false);
  assert.equal(session.recoveryRequired, false);
  await session.close();
});

function sse(...events: Item[]): string {
  return events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("");
}
const answer = sse({ type: "response.output_text.delta", delta: "ok" }, { type: "response.completed", response: {
  output: [{ type: "message", content: [{ type: "output_text", text: "ok" }] }], usage: { input_tokens: 11, output_tokens: 7 } } });

/** An OpenAI provider whose HTTP exchanges are scripted, and an agent whose retry waits are recorded, not slept. */
function retrying(responses: (() => Response)[], sleep?: (ms: number, signal: AbortSignal) => Promise<unknown>) {
  const waits: number[] = [];
  let requests = 0;
  const provider = new OpenAIProvider({ apiKey: "offline", fetch: async () => {
    const respond = responses[requests++];
    assert.ok(respond, "unexpected extra request");
    return respond();
  } });
  const agent = new Agent({ model: "m", provider, sleep: sleep ?? (async (ms) => { waits.push(ms); }) });
  return { agent, waits, requests: () => requests };
}

test("D16 provider retry: 429 then success, no retry for 401 or a broken stream, cancellation stops the wait", async () => {
  const status = (code: number, headers: Record<string, string> = {}) => () => new Response("{\"error\":\"x\"}", { status: code, headers });
  const ok = () => new Response(answer);
  const once = retrying([status(429), ok]);
  const result = await once.agent.run("go");
  assert.equal(result.text, "ok");
  assert.deepEqual([once.requests(), once.waits], [2, [1000]]);
  assert.deepEqual(result.usage, { inputTokens: 11, outputTokens: 7, requests: 1 });

  // Backoff doubles from one second; Retry-After is honoured up to 30 seconds; a connection error is retried too.
  const several = retrying([status(503), status(502, { "Retry-After": "7" }), () => { throw new TypeError("fetch failed"); }, ok]);
  assert.equal((await several.agent.run("go")).text, "ok");
  assert.deepEqual(several.waits, [1000, 7000, 4000]);
  const capped = retrying([status(500, { "Retry-After": "600" }), ok]);
  await capped.agent.run("go");
  assert.deepEqual(capped.waits, [30000]);
  const exhausted = retrying([status(504), status(504), status(504), status(504)]);
  await assert.rejects(exhausted.agent.run("go"), /API error 504/);
  assert.deepEqual([exhausted.requests(), exhausted.waits], [4, [1000, 2000, 4000]]);

  for (const code of [401, 400, 404]) {
    const refused = retrying([status(code)]);
    await assert.rejects(refused.agent.run("go"), new RegExp(`API error ${code}`));
    assert.deepEqual([refused.requests(), refused.waits], [1, []]);
  }

  // The response body has begun, so the request is never sent again.
  const broken = retrying([() => new Response(new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(new TextEncoder().encode(sse({ type: "response.output_text.delta", delta: "par" }))); },
    pull(controller) { controller.error(new TypeError("connection reset")); },
  }))]);
  const events = await collect((onEvent) => broken.agent.run("go", [], { onEvent }));
  assert.deepEqual(events.map((event) => event.type), ["agent_start", "text_delta", "agent_end"]);
  assert.deepEqual([broken.requests(), broken.waits], [1, []]);

  const controller = new AbortController();
  const waiting = deferred<void>();
  const cancelled = retrying([status(429), ok], (_ms, signal) => new Promise((_resolve, reject) => {
    waiting.resolve();
    signal.addEventListener("abort", () => reject(new Error("woken")), { once: true });
  }));
  const run = cancelled.agent.run("go", [], { signal: controller.signal });
  await waiting.promise;
  controller.abort();
  await assert.rejects(run, { name: "AbortError" });
  assert.equal(cancelled.requests(), 1);

  // The default wait is a real timer that cancellation interrupts at once.
  const real = new Agent({ model: "m", provider: new ScriptedProvider([{ error: new ProviderError("busy", { retryable: true, retryAfter: 30 }) }]) });
  const started = Date.now();
  await assert.rejects(real.run("go", [], { signal: AbortSignal.timeout(20) }), { name: "AbortError" });
  assert.ok(Date.now() - started < 2000);
});

function alive(pid: number): boolean {
  try { process.kill(pid, 0); return true; } catch { return false; }
}

test("H09 D17 a bash timeout returns the partial output and leaves no process behind", async () => {
  const started = Date.now();
  let failure: unknown;
  await runBash("echo begun; sleep 30 & echo $!; echo $$; wait", undefined, { timeoutSeconds: 0.4 }).catch((error) => { failure = error; });
  assert.ok(failure instanceof ToolError);
  assert.ok(Date.now() - started < 3000);
  const match = /^exit_code: 124\nstdout:\nbegun\n(\d+)\n(\d+)\nstderr:\n\[timed out after 0\.4 s\]$/.exec(failure.message);
  assert.ok(match, failure.message);
  await until(() => !alive(Number(match[1])) && !alive(Number(match[2])));
  // Through the tool, the model sees it as a tool error and can go on.
  const provider = new ScriptedProvider([call("bash", { command: "echo partial; sleep 30" }), "moved on"]);
  const result = await new Agent({ model: "scripted", provider, tools: [bashTool({ timeoutSeconds: 0.3 })] }).run("go");
  assert.equal(result.text, "moved on");
  assert.deepEqual([provider.requests[1].history.at(-1)?.isError, provider.requests[1].history.at(-1)?.output],
    [true, "exit_code: 124\nstdout:\npartial\nstderr:\n[timed out after 0.3 s]"]);
  assert.match(await runBash("echo quick", undefined, { timeoutSeconds: 5 }), /quick/);
});

test("A5 cancelling as bash enters a foreground command kills its whole process group", { skip: process.platform === "win32" }, async (t) => {
  const directory = scratch(t);
  const pidFile = join(directory, "bash-pid");
  const started = join(directory, "started");
  const controller = new AbortController();
  let pid: number | undefined;
  t.after(() => {
    controller.abort();
    if (pid) try { process.kill(-pid, "SIGKILL"); } catch { /* already gone */ }
  });
  const running = runBash(`printf %s $$ > ${JSON.stringify(pidFile)}; : > ${JSON.stringify(started)}; sleep 30`,
    controller.signal);
  await until(() => existsSync(started));
  pid = Number(readFileSync(pidFile, "utf8"));
  controller.abort();
  await assert.rejects(running);
  await until(() => {
    try { process.kill(-pid!, 0); return false; } catch { return true; }
  });
});

test("D19 bash cwd and env are applied", async (t) => {
  const directory = realpathSync(scratch(t));
  const bash = bashTool({ cwd: directory, env: { TINYAGENT_TEST_VALUE: "from options" } });
  const output = await bash.execute({ command: "pwd; echo \"$TINYAGENT_TEST_VALUE\"; test -n \"$PATH\" && echo inherited" },
    new AbortController().signal);
  assert.equal(output, `exit_code: 0\nstdout:\n${directory}\nfrom options\ninherited\nstderr:\n`);
  assert.match(await runBash("echo \"[$TINYAGENT_TEST_VALUE]\""), /\[\]/, "the options did not leak into the process");
});

/** Run the CLI in this process with a scripted model. */
async function cli(t: TestContext, argv: string[], script: ScriptedTurn[], typed = "", env: Record<string, string> = {}) {
  const provider = new ScriptedProvider(script);
  const stdin = new PassThrough();
  const output = { stdout: "", stderr: "" };
  const sink = (key: "stdout" | "stderr") => { const stream = new PassThrough(); stream.on("data", (chunk: Buffer) => { output[key] += chunk.toString(); }); return stream; };
  stdin.end(typed);
  const code = await runCli({ argv, env: { TINYAGENT_SESSION_DIR: join(scratch(t), "sessions"), ...env }, provider, stdin,
    stdout: sink("stdout"), stderr: sink("stderr") });
  const observations = provider.requests.map((request) => request.history.at(-1)).filter((item) => item?.role === "tool_result")
    .map((item) => String(item?.output));
  return { code, ...output, provider, observations };
}

test("D20 approval: deny with a reason, allow all, --allow, and one-shot without --yolo", async (t) => {
  // The callback: a denial with a reason is a tool error the model sees; the blacklist is not asked about.
  const asked: string[] = [];
  const bash = bashTool({ approve: (command) => { asked.push(command); return command.includes("two") ? true : command.includes("three") ? false : "too risky"; } });
  const provider = new ScriptedProvider([call("bash", { command: "echo one" }), call("bash", { command: "echo two" }),
    call("bash", { command: "echo three" }), call("bash", { command: "rm -rf /tmp/x" }), "done"]);
  const events: AgentEvent[] = [];
  await new Agent({ model: "scripted", provider, tools: [bash] }).run("go", [], { onEvent: (event) => { events.push(event); } });
  const ends = events.flatMap((event) => event.type === "tool_execution_end" ? [[event.isError, event.output]] : []);
  assert.deepEqual(ends[0], [true, "Rejected by user: too risky"]);
  assert.deepEqual(ends[1], [false, "exit_code: 0\nstdout:\ntwo\nstderr:\n"]);
  assert.deepEqual(ends[2], [true, "Rejected by user"]);
  assert.match(String(ends[3][1]), /blocked: 'rm' is denied/);
  assert.equal(ends[3][0], true);
  assert.deepEqual(asked, ["echo one", "echo two", "echo three"]);
  assert.equal(provider.requests[1].history.at(-1)?.output, "Rejected by user: too risky");

  // The interactive CLI asks: a typed reason rejects, "a" allows this and everything after, "n" and "y" do as they say.
  const script = (...commands: string[]): ScriptedTurn[] => [...commands.map((command, index) => call("bash", { command }, `c${index}`)), "finished"];
  const interactive = await cli(t, [], script("echo one", "echo two", "echo three"), "do things\nuse printf instead\na\n");
  assert.equal(interactive.code, 0);
  assert.deepEqual(interactive.observations, ["Rejected by user: use printf instead", "exit_code: 0\nstdout:\ntwo\nstderr:\n",
    "exit_code: 0\nstdout:\nthree\nstderr:\n"]);
  assert.equal(interactive.stderr.split("Run it?").length - 1, 2);
  assert.match(interactive.stdout, /finished/);
  const yesNo = await cli(t, [], script("echo one", "echo two"), "go\nn\ny\n");
  assert.deepEqual(yesNo.observations, ["Rejected by user", "exit_code: 0\nstdout:\ntwo\nstderr:\n"]);
  // --yolo and TINYAGENT_YOLO=1 never ask.
  for (const [argv, env] of [[["--yolo"], {}], [[], { TINYAGENT_YOLO: "1" }]] as const) {
    const yolo = await cli(t, [...argv], script("echo one"), "go\n", env);
    assert.deepEqual([yolo.observations, yolo.stderr.includes("Run it?")], [["exit_code: 0\nstdout:\none\nstderr:\n"], false]);
  }
  // --allow approves matching commands without asking and may be repeated; the rest are still asked about.
  const allow = await cli(t, ["--allow", "echo .*", "--allow", "true"], script("echo one", "true", "printf no"), "go\nn\n");
  assert.deepEqual(allow.observations, ["exit_code: 0\nstdout:\none\nstderr:\n", "exit_code: 0\nstdout:\nstderr:\n", "Rejected by user"]);
  assert.equal(allow.stderr.split("Run it?").length - 1, 1);
  // A pattern must match the whole command, and never approves one that chains, substitutes or redirects.
  for (const sneaky of ["echo one; printf no", "echo $(printf no)", "echo one | cat", "echo one > out", "printf echo one", "echo one\nprintf no"]) {
    const refused = await cli(t, ["-p", "go", "--allow", "echo .*"], script(sneaky));
    assert.match(refused.observations[0], /^Rejected by user/, sneaky);
  }

  // One-shot mode cannot ask: without --yolo or a matching --allow every command is rejected, with a message that says so.
  const oneShot = await cli(t, ["-p", "go"], script("echo one"));
  assert.deepEqual(oneShot.observations, ["Rejected by user: one-shot mode runs a command only with --yolo or a matching --allow"]);
  const allowed = await cli(t, ["--prompt", "go", "--allow", "echo one"], script("echo one", "echo two"));
  assert.deepEqual(allowed.observations.map((observation) => observation.startsWith("exit_code: 0")), [true, false]);
  const blocked = await cli(t, ["-p", "go", "--yolo"], script("echo one", "sudo true"));
  assert.match(blocked.observations[0], /^exit_code: 0/);
  assert.match(blocked.observations[1], /blocked: 'sudo' is denied/, "the blacklist applies in every mode");
});

test("D21 one-shot -p exits 0 when the run completes and 1 otherwise", (t) => {
  const directory = scratch(t);
  const run = (script: ScriptedTurn[], ...argv: string[]) => spawnSync(process.execPath, [here("./cli_fixture.ts"), ...argv], {
    encoding: "utf8", cwd: directory,
    env: { PATH: process.env.PATH, TINYAGENT_SESSION_DIR: join(directory, "sessions"), TINYAGENT_FIXTURE_SCRIPT: JSON.stringify(script) } });
  const tool = call("bash", { command: "pwd" });
  const completed = run([tool, { text: "It works.", deltas: ["It ", "works."] }], "-p", "check", "--yolo", "--cwd", tmpdir());
  assert.equal(completed.status, 0, completed.stderr);
  assert.equal(completed.stdout, "It works.\n");
  // Node 22 may print its node:sqlite warning between the two.
  assert.match(completed.stderr, /\$ pwd\n/);
  assert.ok(completed.stderr.includes(`exit_code: 0\nstdout:\n${realpathSync(tmpdir())}\n`), "--cwd is where bash runs");
  const failed = run([], "--prompt", "check");
  assert.equal(failed.status, 1);
  assert.match(failed.stderr, /^error: /m);
  for (const [flag, name] of [["--max-steps", "maxSteps"], ["--max-tokens", "maxTokens"], ["--max-seconds", "maxSeconds"]]) {
    const limited = run([tool, "unreachable"], "-p", "check", "--yolo", flag, flag === "--max-steps" ? "1" : "0");
    assert.equal(limited.status, 1);
    assert.match(limited.stderr, new RegExp(`error: run exceeded ${name}`));
  }
  const fromEnvironment = spawnSync(process.execPath, [here("./cli_fixture.ts"), "-p", "check"], { encoding: "utf8", cwd: directory,
    env: { PATH: process.env.PATH, TINYAGENT_SESSION_DIR: join(directory, "sessions"), TINYAGENT_MAX_STEPS: "0", TINYAGENT_FIXTURE_SCRIPT: "[\"x\"]" } });
  assert.equal(fromEnvironment.status, 1);
  assert.match(fromEnvironment.stderr, /maxSteps \(0\)/);
  // The sessions went where TINYAGENT_SESSION_DIR said, and the one-shot run is a real, stored session.
  assert.equal(new FileStorage(join(directory, "sessions")).list().length, 6);
});

function lines(text: string): Item[] {
  return text.trimEnd().split("\n").filter(Boolean).map((line) => object(JSON.parse(line)));
}

test("D22 a detached HTTP run survives its client; without detach the disconnect cancels it", { timeout: 10000 }, async (t) => {
  for (const detach of [true, false]) {
    const started = deferred<void>();
    const release = deferred<void>();
    let signal: AbortSignal | undefined;
    const fixture = new HttpFixture(new Agent({ model: "scripted", provider: new ScriptedProvider([async (request) => {
      signal = request.signal;
      started.resolve();
      await Promise.race([release.promise, new Promise((resolve) => request.signal.addEventListener("abort", resolve))]);
      return "finished anyway";
    }]) }));
    t.after(() => fixture.dispose());
    const id = await fixture.session();
    const response = await fixture.send("POST", `/sessions/${id}/prompts`, JSON.stringify({ message: "work", ...(detach ? { detach } : {}) }),
      { "Content-Type": "application/json" });
    assert.equal(response.statusCode, 200);
    await started.promise;
    response.destroy();
    await delay(50);
    assert.equal(signal?.aborted, !detach);
    const state = async () => json(await fixture.send("GET", `/sessions/${id}`));
    if (detach) {
      // Still running: a second prompt is refused, and cancel would reach it.
      assert.equal((await fixture.prompt(id, "second")).statusCode, 409);
      release.resolve();
      let page = await state();
      for (let attempt = 0; attempt < 100 && (page.history as Item[]).length < 2; attempt++) { await delay(10); page = await state(); }
      assert.deepEqual((page.history as Item[]).map((item) => item.role), ["user", "assistant"]);
      const replay = await json(await fixture.send("GET", `/sessions/${id}/events?after=0&limit=100`));
      assert.equal((replay.events as Item[]).at(-1)?.text, "finished anyway");
    } else {
      let page = await state();
      for (let attempt = 0; attempt < 100 && page.resumable !== false; attempt++) { await delay(10); page = await state(); }
      await delay(30);
      assert.deepEqual((await state()).history, []);
      release.resolve();
    }
  }
  // A detached run is still cancelled by cancel.
  const fixture = new HttpFixture(new Agent({ model: "scripted", provider: new ScriptedProvider([async (request) => {
    await new Promise((resolve) => request.signal.addEventListener("abort", resolve));
    return "late";
  }]) }));
  t.after(() => fixture.dispose());
  const id = await fixture.session();
  const response = await fixture.send("POST", `/sessions/${id}/prompts`, JSON.stringify({ message: "work", detach: true }), { "Content-Type": "application/json" });
  const cancelled = await json(await fixture.send("POST", `/sessions/${id}/cancel`));
  assert.equal(cancelled.accepted, true);
  assert.equal(lines(await body(response)).at(-1)?.status, "aborted");
  const invalid = await fixture.send("POST", `/sessions/${id}/prompts`, JSON.stringify({ message: "x", detach: "yes" }), { "Content-Type": "application/json" });
  assert.equal(invalid.statusCode, 400);
});

test("D23 following a run mid-turn: replay, the buffered deltas, then live events, none twice", { timeout: 10000 }, async (t) => {
  const midTurn = deferred<void>();
  const proceed = deferred<void>();
  const provider = new ScriptedProvider([
    { text: "Hello ", deltas: ["Hel", "lo "], toolCalls: [{ callId: "c1", name: "echo", arguments: { text: "a" } }] },
    async function () { return { text: "world", deltas: ["wor", "ld"] }; },
  ]);
  // Hold the second turn open after its first delta.
  const held = { stream: async function* (request: Parameters<ScriptedProvider["stream"]>[0]) {
    const inner = provider.stream(request);
    let count = 0;
    while (true) {
      const next = await inner.next();
      if (next.done) return next.value;
      yield next.value;
      if (provider.requests.length === 2 && ++count === 1) { midTurn.resolve(); await proceed.promise; }
    }
  } };
  const fixture = new HttpFixture(new Agent({ model: "scripted", tools: [echo()], provider: held }));
  t.after(() => fixture.dispose());
  const id = await fixture.session();
  assert.equal(await body(await fixture.send("GET", `/sessions/${id}/events?follow=1`)), "",
    "with no run and no history a follow ends at once");
  const prompt = await fixture.send("POST", `/sessions/${id}/prompts`, JSON.stringify({ message: "go", detach: true }),
    { "Content-Type": "application/json" });
  const primary = body(prompt);
  await midTurn.promise;
  await delay(20);
  const following = await fixture.send("GET", `/sessions/${id}/events?after=0&follow=1`);
  assert.equal(following.headers["content-type"], "application/jsonl; charset=utf-8");
  const partial = await fixture.send("GET", `/sessions/${id}/events?after=3&follow=1`);
  await delay(20);
  proceed.resolve();
  const followed = lines(await body(following));
  const live = lines(await primary);
  assert.deepEqual(followed.map((event) => [event.type, event.delta ?? event.status ?? "", typeof event.cursor]), [
    ["agent_start", "", "number"],
    ["text_delta", "Hello ", "number"],
    ["tool_execution_start", "", "number"],
    ["tool_execution_end", "", "number"],
    ["text_delta", "wor", "undefined"],
    ["text_delta", "ld", "undefined"],
    ["agent_end", "completed", "number"],
  ]);
  // Live events that stand for a journal record carry its cursor, the same one a replay gives them.
  assert.deepEqual(live.map((event) => event.cursor), [2, undefined, undefined, 4, 5, undefined, undefined, 7]);
  assert.deepEqual(followed.slice(0, 4).map((event) => event.cursor), [2, 3, 4, 5]);
  assert.deepEqual(lines(await body(partial)).map((event) => event.type),
    ["tool_execution_start", "tool_execution_end", "text_delta", "text_delta", "agent_end"]);
  // The run's own connection saw every live event once, in order.
  assert.deepEqual(live.map((event) => event.type), ["agent_start", "text_delta", "text_delta", "tool_execution_start",
    "tool_execution_end", "text_delta", "text_delta", "agent_end"]);
  assert.deepEqual(live.map((event) => event.sequence), live.map((_, index) => index + 1));
  // Afterwards the same route replays the whole run from the journal and ends.
  const after = lines(await body(await fixture.send("GET", `/sessions/${id}/events?follow=1`)));
  assert.deepEqual(after.map((event) => event.type), ["agent_start", "text_delta", "tool_execution_start", "tool_execution_end",
    "text_delta", "agent_end"]);
  assert.ok(after.every((event) => typeof event.cursor === "number"));
  const page = await json(await fixture.send("GET", `/sessions/${id}/events?after=5&limit=1`));
  assert.deepEqual([(page.events as Item[]).map((event) => event.delta), page.nextAfter], [["world"], 6]);
  assert.equal((await json(await fixture.send("GET", `/sessions/${id}`))).resumable, false);
});

test("D06 over HTTP and RPC: resume an interrupted run, and the new error codes", async (t) => {
  const crashed = crash(t, "after_result");
  const log = join(crashed.directory, "effects.log");
  const { agent } = resumeAgent(log, { turns: 1 });
  const fixture = new HttpFixture(agent, { directory: crashed.directory });
  t.after(() => fixture.dispose());
  const opened = await fixture.send("POST", "/sessions", JSON.stringify({ sessionId: "s_crash" }), { "Content-Type": "application/json" });
  assert.equal(opened.statusCode, 201);
  await body(opened);
  assert.equal((await json(await fixture.send("GET", "/sessions/s_crash"))).resumable, true);
  const refused = await fixture.prompt("s_crash", "new work");
  assert.equal(refused.statusCode, 409);
  assert.equal(object((await json(refused)).error).code, "resume_required");
  const resumed = await fixture.send("POST", "/sessions/s_crash/resume");
  assert.equal(resumed.statusCode, 200);
  const events = lines(await body(resumed));
  assert.deepEqual(events.map((event) => event.type), ["agent_start", "text_delta", "agent_end"]);
  assert.equal(events[0].resumed, true);
  assert.deepEqual(object(events.at(-1)?.usage), { inputTokens: 0, outputTokens: 0, requests: 1 });
  const nothing = await fixture.send("POST", "/sessions/s_crash/resume");
  assert.equal(nothing.statusCode, 409);
  assert.equal(object((await json(nothing)).error).code, "nothing_to_resume");
  await fixture.dispose();

  const again = crash(t, "after_result");
  const emitted: Item[] = [];
  const rpc = new RpcSession(resumeAgent(log, { turns: 1 }).agent, (value) => { emitted.push(value as Item); }, () => {},
    { directory: again.directory, sessionId: "s_crash", resume: true });
  await rpc.handle({ id: "s", type: "get_state" });
  assert.equal(object(emitted[0].data).resumable, true);
  await rpc.handle({ id: "p", type: "prompt", message: "new work" });
  assert.equal(object(emitted[1].error).code, "resume_required");
  await rpc.handle({ id: "r", type: "resume_run" });
  await rpc.waitIdle();
  assert.deepEqual(emitted.slice(2).map((item) => item.type), ["response", "agent_start", "text_delta", "agent_end"]);
  assert.equal(object(emitted[2].data).runId, emitted[3].runId);
  await rpc.handle({ id: "r2", type: "resume_run" });
  assert.equal(object(emitted.at(-1)?.error).code, "nothing_to_resume");
  await rpc.close();
});

test("D24 close() returns when a stream was left suspended", { timeout: 5000 }, async () => {
  const session = new Session(new Agent({ model: "scripted", provider: new ScriptedProvider(["answer"]) }));
  const events = session.stream("go");
  await events.next();
  await session.close();
  assert.equal(session.records.at(-1)?.type, "run_failed");
  assert.deepEqual(await events.next(), { done: true, value: undefined });
  // A run in flight is cancelled and reports it.
  const waiting = new Session(new Agent({ model: "scripted", provider: new ScriptedProvider([async (request) => {
    await new Promise((resolve) => request.signal.addEventListener("abort", resolve));
    return "late";
  }]) }));
  const running = waiting.run("go");
  await delay(5);
  await waiting.close();
  await assert.rejects(running, { name: "AbortError" });
});
