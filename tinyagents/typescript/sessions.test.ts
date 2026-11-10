import assert from "node:assert/strict";
import { test } from "node:test";
import { appendFileSync, mkdtempSync, rmSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { once } from "node:events";
import fs from "node:fs";
import { syncBuiltinESMExports } from "node:module";
import { RpcSession } from "./rpc.ts";
import { HttpFixture, json, body, object } from "./http_fixture.ts";
import { deferred } from "./provider_fixture.ts";
import { Agent, Session, Tool, SessionBusyError, SessionCorruptionError, RecoveryRequiredError,
  type Item, type Provider, type ProviderRequest, type ProviderTurn, type ProviderEvent, type ContextBudget,
  type AgentEvent } from "./tiny_agent.ts";

class OfflineProvider implements Provider {
  requests: ProviderRequest[] = [];
  /** The history of each request as it was sent; the run keeps appending to request.history. */
  sent: Item[][] = [];
  summary = "The user needs a report. Continue the report.";
  summaryFailure: Error | undefined;
  async *stream(request: ProviderRequest): AsyncGenerator<ProviderEvent, ProviderTurn> {
    this.requests.push(request);
    this.sent.push(structuredClone(request.history));
    const summarizing = request.instructions.startsWith("Summarize");
    if (summarizing && this.summaryFailure) throw this.summaryFailure;
    const text = summarizing ? this.summary : "answer ".repeat(100);
    yield { type: "text_delta", delta: text };
    return { text, assistant: { role: "assistant", content: text }, output: [], toolCalls: [] };
  }
}

/** Every prompt costs one tool call before its answer. */
class ToolTurnProvider extends OfflineProvider {
  override async *stream(request: ProviderRequest): AsyncGenerator<ProviderEvent, ProviderTurn> {
    if (request.instructions.startsWith("Summarize") || request.history.at(-1)?.role === "tool_result")
      return yield* super.stream(request);
    this.requests.push(request);
    this.sent.push(structuredClone(request.history));
    return { text: "", assistant: { role: "assistant", content: [] }, output: [],
      toolCalls: [{ callId: `c${this.requests.length}`, name: "lookup", arguments: {} }] };
  }
}

function outcomes(events: AgentEvent[]): string[] {
  return events.map((event) => event.type === "compaction_end" || event.type === "agent_end"
    ? `${event.type} ${event.status}` : event.type);
}

function lookup(output = "found"): Tool {
  return new Tool({ name: "lookup", description: "Look up", parameters: { type: "object" }, handler: () => output });
}

/** Append a well-formed, correctly checksummed record, as a buggy or older writer could. */
function forge(path: string, record: Item): void {
  const payload = JSON.stringify(record);
  appendFileSync(path, `${JSON.stringify({ payload, sha256: createHash("sha256").update(payload).digest("hex") })}\n`);
}

const budget: ContextBudget = { maxTokens: 10000, responseReserve: 100,
  countTokens: (request) => JSON.stringify(request).length, keepTurns: 1 };
const scratch = tmpdir();
const source = new URL("./tiny_agent.ts", import.meta.url).href;

test("sessions reopen in a fresh process and reject a second writer", async (t) => {
  const directory = mkdtempSync(join(scratch, "ta-session-"));
  t.after(() => rmSync(directory, { recursive: true }));
  const agent = new Agent({ model: "offline", instructions: "Keep facts.", provider: new OfflineProvider() });
  const session = new Session(agent, { directory });
  await session.run("first");
  assert.deepEqual(Session.list(directory), [session.id]);
  assert.throws(() => Session.open(agent, directory, session.id), SessionBusyError);
  await session.close();
  const script = join(directory, "resume.mjs");
  writeFileSync(script, `import { Agent, Session } from ${JSON.stringify(source)};
const agent = new Agent({model: 'offline', instructions: 'Keep facts.', transport: () => []});
const s = Session.open(agent, process.argv[2], process.argv[3]);
if (s.history.length !== 2 || s.history[0].content !== 'first') throw Error('history lost');
await s.close();\n`);
  const child = spawnSync(process.execPath, [script, directory, session.id], { encoding: "utf8" });
  assert.equal(child.status, 0, child.stderr);
  const resumed = Session.open(agent, directory, session.id);
  try {
    await resumed.run("second");
    assert.equal(resumed.history.length, 4);
  } finally { await resumed.close(); }
});

test("torn final writes recover, complete corrupt records fail without repair", async (t) => {
  const directory = mkdtempSync(join(scratch, "ta-session-"));
  t.after(() => rmSync(directory, { recursive: true }));
  const agent = new Agent({ model: "offline", provider: new OfflineProvider() });
  const session = new Session(agent, { directory });
  await session.run("first");
  await session.close();
  const path = join(directory, `${session.id}.jsonl`);
  const original = readFileSync(path);
  writeFileSync(path, Buffer.concat([original, Buffer.from('{"incomplete":')]));
  const resumed = Session.open(agent, directory, session.id);
  assert.equal(resumed.history.length, 2);
  await resumed.close();
  assert.deepEqual(readFileSync(path), original);
  const corrupt = Buffer.concat([original, Buffer.from('{}\n')]);
  writeFileSync(path, corrupt);
  assert.throws(() => Session.open(agent, directory, session.id), SessionCorruptionError);
  assert.deepEqual(readFileSync(path), corrupt);
});

test("tool intent precedes execution and results survive failed event delivery", async (t) => {
  const directory = mkdtempSync(join(scratch, "ta-session-"));
  t.after(() => rmSync(directory, { recursive: true }));
  let calls = 0;
  const agent = new Agent({ model: "offline", provider: {
    async *stream() {
      yield* [];
      return { text: "", assistant: { role: "assistant", content: [] }, output: [],
        toolCalls: [{ callId: "c", name: "effect", arguments: {} }] };
    },
  }, tools: [new Tool({ name: "effect", description: "Effect", parameters: { type: "object" }, handler: () => {
    assert.equal(session.records.at(-1)?.type, "tool_intent");
    calls += 1;
    return "effect happened";
  } })] });
  const session = new Session(agent, { directory });
  const failure = new Error("consumer failed");
  await assert.rejects(session.run("go", { onEvent: (event) => {
    if (event.type === "tool_execution_end") {
      assert.deepEqual(session.records.at(-1)?.item,
        { role: "tool_result", callId: "c", toolName: "effect", output: "effect happened", isError: false });
      throw failure;
    }
  } }), (error) => error === failure);
  assert.deepEqual(session.history, []);
  await session.close();
  const resumed = Session.open(agent, directory, session.id);
  try {
    assert.equal(resumed.recoveryRequired, true);
    await assert.rejects(resumed.run("retry"), RecoveryRequiredError);
    assert.equal(calls, 1);
    resumed.reconcile("Checked the effect. It completed; do not repeat it.");
    assert.equal(resumed.recoveryRequired, false);
  } finally { await resumed.close(); }
});

test("terminal cancellation and early stream closure do not commit", async () => {
  const session = new Session(new Agent({ model: "offline", provider: new OfflineProvider() }));
  try {
    const controller = new AbortController();
    await assert.rejects(session.run("go", { signal: controller.signal, onEvent: (event) => {
      if (event.type === "agent_end") controller.abort();
    } }), { name: "AbortError" });
    assert.deepEqual(session.history, []);
    assert.equal(session.records.at(-1)?.type, "run_failed");
    const events = session.stream("early");
    await events.next();
    await events.return(undefined as never);
    assert.deepEqual(session.history, []);
    assert.equal(session.records.at(-1)?.type, "run_failed");
    assert.equal(session.recoveryRequired, false);
  } finally { await session.close(); }
});

test("compaction preserves raw history, reopens its checkpoint, and updates incrementally", async (t) => {
  const directory = mkdtempSync(join(scratch, "ta-session-"));
  t.after(() => rmSync(directory, { recursive: true }));
  const provider = new OfflineProvider();
  const agent = new Agent({ model: "offline", provider });
  const session = new Session(agent, { directory, budget });
  for (const prompt of ["first", "second", "third"]) await session.run(prompt);
  const original = session.history;
  assert.equal(await session.compact(), true);
  assert.deepEqual(session.history, original);
  assert.equal(session.records.at(-1)?.covered, 4);
  await session.close();
  const resumed = Session.open(agent, directory, session.id, budget);
  try {
    await resumed.run("fourth");
    assert.match(String(provider.requests.at(-1)?.history[0].content), /Continue the report/);
    assert.equal(await resumed.compact(), true);
    const material: unknown = JSON.parse(String(provider.requests.at(-1)?.history[0].content));
    assert.deepEqual(material, { previousSummary: provider.summary, transcript: original.slice(4) });
    assert.deepEqual(provider.requests.at(-1)?.tools, []);
  } finally { await resumed.close(); }
});

test("invalid or cancelled compaction installs nothing", async () => {
  const provider = new OfflineProvider();
  const session = new Session(new Agent({ model: "offline", provider }), { budget });
  try {
    await session.run("one");
    await session.run("two");
    const before = session.records;
    provider.summary = " ";
    await assert.rejects(session.compact(), /nonempty/);
    assert.deepEqual(session.records, before);
    await assert.rejects(session.compact({ signal: AbortSignal.abort() }), { name: "AbortError" });
    assert.deepEqual(session.records, before);
  } finally { await session.close(); }
});

test("process crash releases the writer lock and never replays uncertain tools", async (t) => {
  const directory = mkdtempSync(join(scratch, "ta-session-"));
  t.after(() => rmSync(directory, { recursive: true }));
  const script = join(directory, "crash.mjs");
  writeFileSync(script, `import { Agent, Session, Tool } from ${JSON.stringify(source)};
const tool = new Tool({name:'effect', description:'Effect', parameters:{type:'object'}, handler:()=>process.exit(29)});
const agent = new Agent({model:'offline', tools:[tool], transport:()=>[{type:'function_call',call_id:'c',name:'effect',arguments:'{}'}]});
const session = new Session(agent, {directory:process.argv[2], sessionId:'s_crashed'});
await session.run('go');\n`);
  const child = spawnSync(process.execPath, [script, directory], { encoding: "utf8" });
  assert.equal(child.status, 29, child.stderr);
  let calls = 0;
  const agent = new Agent({ model: "offline", provider: new OfflineProvider(), tools: [new Tool({
    name: "effect", description: "Effect", parameters: { type: "object" }, handler: () => { calls += 1; },
  })] });
  const resumed = Session.open(agent, directory, "s_crashed");
  try {
    assert.equal(resumed.recoveryRequired, true);
    await assert.rejects(resumed.run("retry"), RecoveryRequiredError);
    assert.equal(calls, 0);
    assert.deepEqual(resumed.history, []);
    assert.equal(resumed.records.at(-1)?.type, "tool_intent");
    resumed.reconcile("The effect did not complete; it is safe to continue.");
  } finally { await resumed.close(); }
  const continued = Session.open(agent, directory, "s_crashed");
  try {
    assert.equal(continued.recoveryRequired, false);
    await continued.run("retry");
    assert.equal(calls, 0);
    assert.deepEqual(continued.history.map((item) => item.role), ["user", "user", "assistant"]);
    assert.match(String(continued.history[0].content), /Interrupted run recovery: The effect did not complete/);
  } finally { await continued.close(); }
});

test("automatic compaction preserves raw turns and event ordering", async () => {
  const provider = new OfflineProvider();
  const session = new Session(new Agent({ model: "offline", provider }), {
    budget: { ...budget, maxTokens: 2500 },
  });
  const emitted: { type: string; runId: string; sequence: number }[] = [];
  try {
    for (let index = 0; index < 5; index++) await session.run(`prompt ${index}`, {
      onEvent: (event) => { emitted.push(event); },
    });
    assert.ok(emitted.some((event) => event.type === "compaction_start"));
    assert.equal(session.history.length, 10);
    assert.equal(provider.requests.at(-1)?.history.at(-2)?.content, "prompt 4");
    for (const id of new Set(emitted.map((event) => event.runId))) {
      const run = emitted.filter((event) => event.runId === id);
      assert.deepEqual(run.map((event) => event.sequence), run.map((_, index) => index + 1));
    }
  } finally { await session.close(); }
});

test("an oversized pending prompt cannot activate an unusable checkpoint", async () => {
  const session = new Session(new Agent({ model: "offline", provider: new OfflineProvider() }), { budget });
  try {
    await session.run("one");
    await session.run("two");
    await assert.rejects(session.run("x".repeat(10000)), /still exceeds/);
    assert.equal(session.records.some((record) => record.type === "checkpoint"), false);
    assert.equal(session.history.length, 4);
  } finally { await session.close(); }
});

test("sessions reject concurrent operations and configuration changes", async () => {
  const agent = new Agent({ model: "offline", provider: new OfflineProvider() });
  const session = new Session(agent);
  try {
    const stream = session.stream("go");
    await stream.next();
    await assert.rejects(session.run("concurrent"), SessionBusyError);
    await stream.return(undefined as never);
    agent.tools.set("new", new Tool({ name: "new", description: "New", parameters: {}, handler: () => "done" }));
    await assert.rejects(session.run("go"), /configuration changed/);
  } finally { await session.close(); }
});

test("checkpoint write failure retains the previous context and poisons the handle", async (t) => {
  const directory = mkdtempSync(join(scratch, "ta-session-"));
  t.after(() => rmSync(directory, { recursive: true }));
  const session = new Session(new Agent({ model: "offline", provider: new OfflineProvider() }), { directory, budget });
  try {
    await session.run("one");
    await session.run("two");
    const before = session.records;
    const mocked = t.mock.method(fs, "fsyncSync", () => { throw new Error("disk full"); });
    syncBuiltinESMExports();
    try { await assert.rejects(session.compact(), /disk full/); }
    finally { mocked.mock.restore(); syncBuiltinESMExports(); }
    assert.deepEqual(session.records, before);
    await assert.rejects(session.run("again"), /failed/);
  } finally { await session.close(); }
});

test("RPC resumes, compacts, inspects, and releases its writer on close", async (t) => {
  const directory = mkdtempSync(join(scratch, "ta-session-"));
  t.after(() => rmSync(directory, { recursive: true }));
  const provider = new OfflineProvider();
  const agent = new Agent({ model: "offline", provider });
  const emitted: object[] = [];
  const rpc = new RpcSession(agent, (event) => { emitted.push(event); }, undefined, { directory, budget });
  const id = rpc.session.id;
  for (let index = 0; index < 3; index++) {
    await rpc.handle({ id: String(index), type: "prompt", message: `turn ${index}` });
    assert.equal(await rpc.waitIdle(2000), true);
  }
  await rpc.handle({ id: "compact", type: "compact" });
  assert.equal(await rpc.waitIdle(2000), true);
  assert.equal(object(emitted.at(-1)).type, "compaction_end");
  assert.equal(object(emitted.at(-1)).status, "completed");
  await rpc.handle({ id: "history", type: "get_history", limit: 1 });
  assert.equal(object(object(emitted.at(-1)).data).nextOffset, 1);
  await rpc.close();
  const resumed = new RpcSession(agent, (event) => { emitted.push(event); }, undefined,
    { directory, sessionId: id, resume: true, budget });
  try {
    await resumed.handle({ id: "go", type: "prompt", message: "continue" });
    assert.equal(await resumed.waitIdle(2000), true);
    assert.match(String(provider.requests.at(-1)?.history[0].content), /Earlier conversation summary/);
  } finally { await resumed.close(); }
});

test("HTTP resumes durable sessions after shutdown and close preserves the journal", async (t) => {
  const directory = mkdtempSync(join(scratch, "ta-session-"));
  t.after(() => rmSync(directory, { recursive: true }));
  const provider = new OfflineProvider();
  const agent = new Agent({ model: "offline", provider });
  const fixture = new HttpFixture(agent, { directory, budget });
  const id = await fixture.session();
  try {
    for (const prompt of ["one", "two", "three"]) await body(await fixture.prompt(id, prompt));
    const compact = await fixture.send("POST", `/sessions/${id}/compact`);
    assert.equal(compact.statusCode, 200);
    const events = (await body(compact)).trimEnd().split("\n").map((line) => object(JSON.parse(line)));
    assert.equal(events.at(-1)?.type, "compaction_end");
    assert.equal(events.at(-1)?.status, "completed");
  } finally { await fixture.dispose(); }
  const resumed = new HttpFixture(agent, { directory, budget });
  try {
    assert.deepEqual((await json(await resumed.send("GET", "/sessions"))).sessionIds, [id]);
    const opened = await resumed.send("POST", "/sessions", JSON.stringify({ sessionId: id }),
      { "Content-Type": "application/json" });
    assert.equal(opened.statusCode, 201);
    await body(opened);
    const page = await json(await resumed.send("GET", `/sessions/${id}?limit=1`));
    assert.equal(page.nextOffset, 1);
    await body(await resumed.prompt(id, "continue"));
    assert.match(String(provider.requests.at(-1)?.history[0].content), /Earlier conversation summary/);
    const removed = await resumed.send("DELETE", `/sessions/${id}`);
    assert.equal(removed.statusCode, 204);
    await body(removed);
    assert.deepEqual(Session.list(directory), [id]);
    const direct = Session.open(agent, directory, id);
    assert.equal(direct.history.length, 8);
    await direct.close();
  } finally { await resumed.dispose(); }
});

test("a session held by another process is busy until that process dies", { timeout: 20000 }, async (t) => {
  const directory = mkdtempSync(join(scratch, "ta-session-"));
  t.after(() => rmSync(directory, { recursive: true }));
  const script = join(directory, "hold.mjs");
  writeFileSync(script, `import { Agent, Session } from ${JSON.stringify(source)};
const agent = new Agent({model: 'offline', transport: () => []});
new Session(agent, {directory: process.argv[2], sessionId: 's_held'});
console.log('holding');
setInterval(() => {}, 1000);\n`);
  const child = spawn(process.execPath, [script, directory], { stdio: ["ignore", "pipe", "inherit"] });
  const exited = once(child, "close");
  const agent = new Agent({ model: "offline", provider: new OfflineProvider() });
  try {
    await Promise.race([once(child.stdout, "data"), exited]);
    assert.throws(() => Session.open(agent, directory, "s_held"), SessionBusyError);
  } finally {
    child.kill("SIGKILL");
    await exited;
  }
  const resumed = Session.open(agent, directory, "s_held");
  await resumed.close();
});

test("damaged complete records are corruption and an unknown ID creates nothing", async (t) => {
  const directory = mkdtempSync(join(scratch, "ta-session-"));
  t.after(() => rmSync(directory, { recursive: true }));
  const agent = new Agent({ model: "offline", provider: new OfflineProvider() });
  const session = new Session(agent, { directory });
  await session.run("first");
  await session.close();
  const path = join(directory, `${session.id}.jsonl`);
  const original = readFileSync(path);
  const lines = original.toString().trimEnd().split("\n");
  const flipped = Buffer.from(original);
  flipped[original.indexOf("first")] ^= 1;
  const damaged = {
    "a changed byte": flipped,
    "a missing record": Buffer.from(`${[...lines.slice(0, 2), ...lines.slice(3)].join("\n")}\n`),
    "invalid UTF-8": Buffer.concat([original, Buffer.from([0xff, 0x0a])]),
  };
  for (const [name, bytes] of Object.entries(damaged)) {
    writeFileSync(path, bytes);
    assert.throws(() => Session.open(agent, directory, session.id), SessionCorruptionError, name);
    assert.deepEqual(readFileSync(path), bytes, name);
  }
  writeFileSync(path, original);
  const repaired = Session.open(agent, directory, session.id);
  assert.equal(repaired.history.length, 2);
  await repaired.close();
  assert.throws(() => Session.open(agent, directory, "s_missing"), { code: "ENOENT" });
  assert.equal(fs.readdirSync(directory).some((name) => name.startsWith("s_missing")), false);
  const other = new Agent({ model: "offline", instructions: "Different.", provider: new OfflineProvider() });
  assert.throws(() => Session.open(other, directory, session.id), /differ from the saved configuration/);
  await Session.open(agent, directory, session.id).close();
});

test("a failed journal write stops the run before its tool executes", async (t) => {
  const directory = mkdtempSync(join(scratch, "ta-session-"));
  t.after(() => rmSync(directory, { recursive: true }));
  let calls = 0;
  const agent = new Agent({ model: "offline", provider: new ToolTurnProvider(), tools: [new Tool({
    name: "lookup", description: "Look up", parameters: { type: "object" }, handler: () => { calls += 1; return "found"; },
  })] });
  const session = new Session(agent, { directory });
  let mocked: { mock: { restore(): void } } | undefined;
  try {
    await assert.rejects(session.run("go", { onEvent: (event) => {
      if (event.type !== "agent_start") return;
      mocked = t.mock.method(fs, "fsyncSync", () => { throw new Error("disk full"); });
      syncBuiltinESMExports();
    } }), /disk full/);
  } finally { mocked?.mock.restore(); syncBuiltinESMExports(); }
  assert.equal(calls, 0);
  await assert.rejects(session.run("again"), /failed/);
  await session.close();
  const resumed = Session.open(agent, directory, session.id);
  try {
    assert.deepEqual(resumed.history, []);
    assert.equal(resumed.recoveryRequired, false);
    await resumed.run("again");
    assert.equal(calls, 1);
  } finally { await resumed.close(); }
});

test("compaction keeps tool calls with their results", async () => {
  const provider = new ToolTurnProvider();
  const session = new Session(new Agent({ model: "offline", provider, tools: [lookup()] }), { budget });
  try {
    for (const prompt of ["first", "second", "third"]) await session.run(prompt);
    assert.equal(await session.compact(), true);
    assert.equal(session.records.at(-1)?.covered, 8);
    await session.run("fourth");
    const request = provider.sent.at(-2) ?? [];
    assert.match(String(request[0].content), /Earlier conversation summary/);
    assert.deepEqual(request.slice(1).map((item) => item.role),
      ["user", "assistant", "tool_result", "assistant", "user"]);
    assert.equal(request[1].content, "third");
    assert.equal(session.history.length, 16);
  } finally { await session.close(); }
});

test("automatic compaction during a tool loop keeps the unfinished exchange", async () => {
  const provider = new ToolTurnProvider();
  const agent = new Agent({ model: "offline", provider, tools: [lookup("x".repeat(1500))] });
  const session = new Session(agent, { budget: { ...budget, maxTokens: 6000 } });
  const emitted: AgentEvent[] = [];
  try {
    await session.run("first");
    await session.run("second");
    await session.run("third", { onEvent: (event) => { emitted.push(event); } });
    assert.deepEqual(outcomes(emitted), ["agent_start", "tool_execution_start", "tool_execution_end",
      "compaction_start", "compaction_end completed", "text_delta", "agent_end completed"]);
    const request = provider.sent.at(-1) ?? [];
    assert.match(String(request[0].content), /Earlier conversation summary/);
    assert.deepEqual(request.slice(1).map((item) => item.role),
      ["user", "assistant", "tool_result", "assistant", "user", "assistant", "tool_result"]);
    assert.equal(request[1].content, "second");
    assert.equal(session.history.length, 12);
  } finally { await session.close(); }
});

test("a failed summary request leaves explicit and automatic compaction unchanged", async () => {
  const provider = new OfflineProvider();
  const session = new Session(new Agent({ model: "offline", provider }), { budget: { ...budget, maxTokens: 1700 } });
  const emitted: AgentEvent[] = [];
  const onEvent = (event: AgentEvent) => { emitted.push(event); };
  try {
    await session.run("one");
    await session.run("two");
    const history = session.history;
    provider.summaryFailure = new Error("summary provider down");
    await assert.rejects(session.compact({ onEvent }), (error) => error === provider.summaryFailure);
    assert.deepEqual(outcomes(emitted), ["compaction_start", "compaction_end failed"]);
    emitted.length = 0;
    await assert.rejects(session.run("three", { onEvent }), (error) => error === provider.summaryFailure);
    assert.deepEqual(outcomes(emitted),
      ["agent_start", "compaction_start", "compaction_end failed", "agent_end failed"]);
    assert.deepEqual(session.history, history);
    assert.equal(session.records.some((record) => record.type === "checkpoint"), false);
    assert.equal(session.records.at(-1)?.type, "run_failed");
    provider.summaryFailure = undefined;
    await session.run("three");
    assert.equal(session.records.filter((record) => record.type === "checkpoint").length, 1);
    assert.equal(session.history.length, 6);
  } finally { await session.close(); }
});

test("cancelling or rejecting a summary in flight installs nothing", async () => {
  const started = deferred<void>();
  const provider = new OfflineProvider();
  const held: Provider = {
    async *stream(request) {
      if (!request.instructions.startsWith("Summarize")) return yield* provider.stream(request);
      started.resolve();
      await new Promise<void>((resolve) => request.signal.addEventListener("abort", () => resolve(), { once: true }));
      return { text: "late summary", assistant: { role: "assistant", content: "late summary" }, output: [], toolCalls: [] };
    },
  };
  const session = new Session(new Agent({ model: "offline", provider: held }), { budget });
  const emitted: AgentEvent[] = [];
  try {
    await session.run("one");
    await session.run("two");
    const before = session.records;
    const controller = new AbortController();
    const compacting = session.compact({ signal: controller.signal, onEvent: (event) => { emitted.push(event); } });
    await started.promise;
    await assert.rejects(session.run("concurrent"), SessionBusyError);
    controller.abort();
    await assert.rejects(compacting, { name: "AbortError" });
    assert.deepEqual(outcomes(emitted), ["compaction_start", "compaction_end aborted"]);
    assert.deepEqual(session.records, before);
  } finally { await session.close(); }
  const plain = new Session(new Agent({ model: "offline", provider }), { budget });
  try {
    await plain.run("one");
    await plain.run("two");
    const before = plain.records;
    provider.summary = "long ".repeat(4000);
    await assert.rejects(plain.compact(), /useful context room/);
    assert.deepEqual(plain.records, before);
    await plain.run("three");
    assert.deepEqual(provider.sent.at(-1)?.map((item) => item.role),
      ["user", "assistant", "user", "assistant", "user"]);
  } finally { await plain.close(); }
});

test("an invalid checkpoint found on resume is ignored and reported", async (t) => {
  const directory = mkdtempSync(join(scratch, "ta-session-"));
  t.after(() => rmSync(directory, { recursive: true }));
  const provider = new OfflineProvider();
  const agent = new Agent({ model: "offline", provider });
  const session = new Session(agent, { directory, budget });
  for (const prompt of ["first", "second", "third"]) await session.run(prompt);
  assert.equal(await session.compact(), true);
  const sequence = session.records.length;
  // The journal hashes covered history as JSON with sorted keys.
  const split = session.history.slice(0, 5).map((item) => ({ content: item.content, role: item.role }));
  await session.close();
  const path = join(directory, `${session.id}.jsonl`);
  forge(path, { type: "checkpoint", covered: 6, summary: "Covers other history.", historyHash: "0".repeat(64),
    version: 1, sequence: sequence + 1 });
  forge(path, { type: "checkpoint", covered: 5, summary: "Splits an exchange.",
    historyHash: createHash("sha256").update(JSON.stringify(split)).digest("hex"), version: 1, sequence: sequence + 2 });
  const resumed = Session.open(agent, directory, session.id, budget);
  try {
    assert.equal(resumed.recoveryNotes.length, 2);
    assert.deepEqual(resumed.inspect().recoveryNotes, resumed.recoveryNotes);
    assert.equal(resumed.history.length, 6);
    await resumed.run("fourth");
    const request = provider.sent.at(-1) ?? [];
    assert.match(String(request[0].content), /Continue the report/);
    assert.equal(request.length, 4);
    assert.equal(request[1].content, "third");
  } finally { await resumed.close(); }
});
