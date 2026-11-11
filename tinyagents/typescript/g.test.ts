/** Part G rulings that have no reproduction file of their own. Titles start with the ruling. */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { HttpFixture, body, json, object } from "./http_fixture.ts";
import { Agent, FencedError, MAX_JSONL_LINE_BYTES, MemoryStorage, OpenAIProvider, RpcSession, ScriptedProvider, Session,
  SessionFailedError, StorageError, Tool, pause, type AgentEvent, type Item, type ScriptedTurn, type Storage } from "./tiny_agent.ts";

const call = (name: string, arguments_: Item = {}, callId = `call_${name}`) => ({ callId, name, arguments: arguments_ });
const tool = (name: string, handler: Tool["handler"], extra: Partial<ConstructorParameters<typeof Tool>[0]> = {}) =>
  new Tool({ name, description: name, parameters: { type: "object" }, handler, ...extra });
const schema = { type: "object", properties: { answer: { type: "number" } }, required: ["answer"] };

/** A store that "crashes" when a record of this type is appended: the session stops, the run stays open. */
function dyingAt(type: string, memory = new MemoryStorage()): Storage & { memory: MemoryStorage } {
  return { memory, load: (id) => memory.load(id), list: () => memory.list(), release: () => {},
    append(id, expected, record) {
      if (record.type === type) throw new Error("the process died here");
      return memory.append(id, expected, record);
    } };
}

test("G02 final_output beside other calls: the others are answered, not run, and history stays valid", async () => {
  let ran = 0;
  const provider = new ScriptedProvider([[call("lookup"), call("final_output", { answer: 42 }), call("lookup", {}, "third")], "next"]);
  const session = new Session(new Agent({ model: "m", provider, tools: [tool("lookup", () => { ran += 1; return "found"; })] }));
  const result = await session.run("go", { outputSchema: schema });
  assert.deepEqual([result.value, ran], [{ answer: 42 }, 0]);
  assert.deepEqual(session.history.slice(2).map((item) => [item.callId, item.isError, item.output]), [
    ["call_lookup", true, "not run: the run finished"], ["call_final_output", false, "Output accepted."],
    ["third", true, "not run: the run finished"]]);
  await session.run("again");
  assert.equal(provider.requests[1].history.filter((item) => item.role === "tool_result").length, 3);
});

test("G04 resume input is validated before anything starts and leaves the pause as it was", async (t) => {
  const provider = new ScriptedProvider([[call("ask")], "thanks"]);
  const agent = new Agent({ model: "m", provider, tools: [tool("ask", () => pause("why?"))] });
  const session = new Session(agent);
  await session.run("go");
  const records = session.records.length;
  await assert.rejects(session.resume({ error: 5 as unknown as string }), TypeError);
  await assert.rejects(session.resume({ result: "x".repeat(200_000) }), /exceeds/);
  await assert.rejects(session.resume(), /paused/);
  assert.deepEqual([session.records.length, session.paused?.toolName], [records, "ask"]);
  // null is a result: the key being present is what counts.
  assert.equal((await session.resume({ result: null })).text, "thanks");
  assert.equal(provider.requests[1].history.at(-1)?.output, "null");
  const fixture = new HttpFixture(new Agent({ model: "m", provider: new ScriptedProvider([[call("ask")]]), tools: [tool("ask", () => pause("why?"))] }));
  t.after(() => fixture.dispose());
  const id = await fixture.session();
  await body(await fixture.prompt(id, "go"));
  const bad = await fixture.send("POST", `/sessions/${id}/resume`, JSON.stringify({ error: { no: "string" } }), { "Content-Type": "application/json" });
  assert.deepEqual([bad.statusCode, object((await json(bad)).error).code], [400, "invalid_request"]);
  assert.notEqual((await json(await fixture.send("GET", `/sessions/${id}`))).paused, null);
});

test("G06 lone surrogates from a prompt, a tool or a model never break the journal or RPC", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "ta-g-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const turns = (): ScriptedTurn[] => [{ text: "half \ud83d", toolCalls: [call("echo", { text: "\udc00 tail" })] }, "done \ud800"];
  const agent = (script: ScriptedTurn[]) => new Agent({ model: "m", provider: new ScriptedProvider(script), tools: [tool("echo", () => "out \ud800 put")] });
  const session = new Session(agent(turns()), { directory });
  const events: AgentEvent[] = [];
  const result = await session.run("prompt \ud800 here", { onEvent: (event) => { events.push(event); } });
  assert.equal(result.text, "done �");
  assert.equal(session.history[0].content, "prompt � here");
  assert.equal(session.history[2].output, "out � put");
  assert.doesNotMatch(JSON.stringify(events), /\\ud[89a-f]/i);
  await session.close();
  const reopened = new Session(agent([]), { directory, sessionId: session.id, resume: true });
  assert.equal(reopened.history.length, 4);
  await reopened.close();
  const emitted: Item[] = [];
  const rpc = new RpcSession(agent(turns()), (value) => { emitted.push(value as Item); }, () => {}, { directory });
  await rpc.handle(JSON.parse('{"id":"p","type":"prompt","message":"lone \\ud800"}') as Item);
  await rpc.waitIdle();
  assert.equal(emitted.at(-1)?.status, "completed");
  await rpc.handle({ id: "s", type: "get_state" });
  assert.equal(object(emitted.at(-1)?.data).status, "idle");
  await rpc.close();
});

test("G07 a default events page fits one line however large the tool results are", async () => {
  const script: ScriptedTurn[] = [...Array.from({ length: 12 }, (_, index) => [call("big", {}, `c${index}`)]), "done"];
  const session = new Session(new Agent({ model: "m", provider: new ScriptedProvider(script), tools: [tool("big", () => "x".repeat(120_000))] }));
  await session.run("go");
  const seen: AgentEvent[] = [];
  let after = 0;
  for (let pages = 0; pages < 20; pages++) {
    const page = session.events({ after });
    assert.ok(Buffer.byteLength(JSON.stringify(page)) <= MAX_JSONL_LINE_BYTES);
    seen.push(...page.events);
    if (page.nextAfter === null) break;
    assert.ok(page.nextAfter > after);
    after = page.nextAfter;
  }
  assert.deepEqual(seen.map((event) => event.cursor), [...seen.map((event) => event.cursor ?? 0)].sort((a, b) => a - b));
  assert.equal(new Set(seen.map((event) => event.cursor)).size, seen.length);
  assert.equal(seen.filter((event) => event.type === "tool_execution_end").length, 12);
  assert.equal(seen.at(-1)?.type, "agent_end");
});

test("G08 closing a stream at any event leaves a pause a pause and anything else an aborted run", async () => {
  const build = () => new Session(new Agent({ model: "m", tools: [tool("ask", () => pause("?")), tool("echo", () => "ok")],
    provider: new ScriptedProvider([[call("echo"), call("ask")], "done"]) }));
  const length = (await (async () => { const events: AgentEvent[] = []; const session = build(); await session.run("go", { onEvent: (event) => { events.push(event); } }); return events; })()).length;
  for (let stop = 1; stop <= length; stop++) {
    const session = build();
    const stream = session.stream("go");
    let last: AgentEvent | undefined;
    for (let index = 0; index < stop; index++) last = (await stream.next()).value as AgentEvent;
    await stream.return(undefined as never);
    const terminal = session.records.at(-1);
    if (last?.type === "agent_end") assert.deepEqual([terminal?.type, session.paused?.toolName], ["run_paused", "ask"]);
    else assert.deepEqual([terminal?.type, terminal?.status, session.resumable], ["run_failed", "aborted", false]);
    // Replay is well-formed either way: every start has an end.
    const replay = session.events({ limit: 1000 }).events;
    assert.equal(replay.filter((event) => event.type === "tool_execution_start").length,
      replay.filter((event) => event.type === "tool_execution_end").length, `stopped after event ${stop}`);
    await session.close();
  }
});

test("G10 a stale writer is fenced before it reruns an idempotent tool", async () => {
  let runs = 0;
  const agent = (script: ScriptedTurn[]) => new Agent({ model: "m", provider: new ScriptedProvider(script),
    tools: [tool("save", () => { runs += 1; return "saved"; }, { idempotent: true })] });
  const dying = dyingAt("tool_result");
  const crashed = new Session(agent([[call("save")]]), { storage: dying });
  await assert.rejects(crashed.run("go"), StorageError);
  assert.equal(runs, 1);
  const [first, second] = [0, 1].map(() => new Session(agent(["done"]), { storage: dying.memory, sessionId: crashed.id, resume: true }));
  assert.deepEqual([first.resumable, second.resumable], [true, true]);
  const events: AgentEvent[] = [];
  await first.resume({ onEvent: (event) => { events.push(event); } });
  assert.equal(runs, 2);
  assert.ok(first.records.some((record) => record.type === "tool_rerun"));
  assert.equal(events.find((event) => event.type === "tool_execution_start")?.cursor, first.records.find((record) => record.type === "tool_rerun")?.sequence);
  await assert.rejects(second.resume(), FencedError);
  assert.equal(runs, 2, "the overtaken writer did not run the tool");
});

test("G11 G28 a store that answers with the wrong sequence, or fails, stops the session with a named error", async (t) => {
  const memory = new MemoryStorage();
  let lie = false;
  const storage: Storage = { load: (id) => memory.load(id), list: () => memory.list(), release: () => {},
    append: (id, expected, record) => memory.append(id, expected, record) + (lie && record.type === "item" ? 1 : 0) };
  const session = new Session(new Agent({ model: "m", provider: new ScriptedProvider(["one", "two"]) }), { storage });
  lie = true;
  const events: AgentEvent[] = [];
  await assert.rejects(session.run("go", { onEvent: (event) => { events.push(event); } }),
    (error) => error instanceof StorageError && error.code === "storage_error" && /sequence/.test(error.message));
  const end = events.at(-1);
  assert.ok(end?.type === "agent_end" && end.status === "failed");
  assert.deepEqual([end.error.code, end.error.retryable], ["storage_error", false]);
  lie = false;
  await assert.rejects(session.run("again"), (error) => error instanceof SessionFailedError && error.code === "session_failed");
  await assert.rejects(session.abandon(), SessionFailedError);
  // Over RPC the failed session answers every command, by name.
  const flaky = dyingAt("item");
  const emitted: Item[] = [];
  const rpc = new RpcSession(new Agent({ model: "m", provider: new ScriptedProvider(["one"]) }), (value) => { emitted.push(value as Item); }, () => {}, { storage: flaky });
  await rpc.handle({ id: "p", type: "prompt", message: "go" });
  await rpc.waitIdle();
  assert.deepEqual([emitted.at(-1)?.status, object(emitted.at(-1)?.error).code], ["failed", "storage_error"]);
  for (const type of ["prompt", "resume_run", "abandon_run", "reconcile"]) {
    await rpc.handle({ id: type, type, message: "x", note: "x" });
    const answer = emitted.find((item) => item.id === type);
    assert.equal(answer?.success, false, type);
  }
  assert.equal(object(emitted.find((item) => item.id === "prompt")?.error).code, "session_failed");
  await rpc.handle({ id: "s", type: "get_state" });
  assert.equal(emitted.at(-1)?.success, true);
  await rpc.close();
  void t;
});

test("G14 a key with a control character is refused by name, and error bodies are cut to 500 characters", async () => {
  let requests = 0;
  const fetch = async () => { requests += 1; return new Response("E".repeat(5000), { status: 400 }); };
  const events: AgentEvent[] = [];
  const session = new Session(new Agent({ model: "m", provider: new OpenAIProvider({ apiKey: "sk-SECRET-VALUE\n", fetch }) }));
  const failure = await session.run("go", { onEvent: (event) => { events.push(event); } }).catch((error: Error) => error);
  assert.ok(failure instanceof Error);
  assert.match(failure.message, /request header authorization contains a control character/);
  assert.equal(requests, 0);
  assert.equal((String(failure.stack) + JSON.stringify(events) + JSON.stringify(session.records)).includes("SECRET-VALUE"), false);
  const header = new Agent({ model: "m", provider: new OpenAIProvider({ apiKey: "k", headers: { "X-Team": "a\rb" }, fetch }) });
  await assert.rejects(header.run("go"), /request header x-team contains a control character/);
  const long = await new Agent({ model: "m", provider: new OpenAIProvider({ apiKey: "k", fetch }) }).run("go").catch((error: Error) => error);
  assert.ok(long instanceof Error && long.message.length < 600 && long.message.includes("E".repeat(500)));
});

test("G16 the HTTP server refuses a foreign Host with 421 and any unlisted Origin with 403", async (t) => {
  const agent = new Agent({ model: "m", provider: new ScriptedProvider([]) });
  const fixture = new HttpFixture(agent, { token: "secret" });
  const open = new HttpFixture(agent, { allowedHosts: ["agent.internal"], allowedOrigins: ["https://app.example"] });
  t.after(() => Promise.all([fixture.dispose(), open.dispose()]));
  const get = async (target: HttpFixture, headers: string[]) => {
    const exchange = target.open("GET", "/health", headers);
    exchange.request.end();
    const response = await exchange.response;
    return [response.statusCode, response.statusCode === 200 ? "ok" : object((await json(response)).error).code];
  };
  const auth = ["Authorization", "Bearer secret"];
  assert.deepEqual(await get(fixture, ["Host", "127.0.0.1:8080", ...auth]), [200, "ok"]);
  assert.deepEqual(await get(fixture, ["Host", "localhost", ...auth]), [200, "ok"]);
  assert.deepEqual(await get(fixture, ["Host", "[::1]:8080", ...auth]), [200, "ok"]);
  // Checked before the token: a rebinding page learns nothing.
  assert.deepEqual(await get(fixture, ["Host", "evil.example"]), [421, "misdirected_request"]);
  assert.deepEqual(await get(fixture, ["Host", "agent.internal", ...auth]), [421, "misdirected_request"]);
  assert.deepEqual(await get(fixture, ["Host", "127.0.0.1", "Origin", "https://evil.example", ...auth]), [403, "origin_not_allowed"]);
  assert.deepEqual(await get(fixture, ["Host", "127.0.0.1", "Origin", "null", ...auth]), [403, "origin_not_allowed"]);
  assert.deepEqual(await get(open, ["Host", "agent.internal"]), [200, "ok"]);
  assert.deepEqual(await get(open, ["Host", "127.0.0.1", "Origin", "https://app.example"]), [200, "ok"]);
  assert.deepEqual(await get(open, ["Host", "127.0.0.1", "Origin", "https://other.example"]), [403, "origin_not_allowed"]);
  const post = open.open("POST", "/sessions", ["Host", "evil.example", "Content-Type", "application/json"]);
  post.request.end("{}");
  assert.equal((await post.response).statusCode, 421);
});

test("G17 a resumed run keeps the model, instructions, extra, schema and limits it was started with", async () => {
  const dying = dyingAt("tool_result");
  const tools = [tool("step", () => "ok")];
  const crashed = new Session(new Agent({ model: "agent-model", provider: new ScriptedProvider([[call("step")]]), tools }), { storage: dying });
  const context = { tenant: "PRIVATE" };
  await assert.rejects(crashed.run("go", { model: "run-model", instructions: "Run instructions.", extra: { temperature: 0 },
    outputSchema: schema, maxSteps: 2, context }), StorageError);
  const stored = object(crashed.records.find((record) => record.type === "run_start")?.options);
  assert.deepEqual(stored, { model: "run-model", instructions: "Run instructions.", extra: { temperature: 0 }, outputSchema: schema, maxSteps: 2 });
  assert.equal(JSON.stringify(crashed.records).includes("PRIVATE"), false, "context is never stored");
  const provider = new ScriptedProvider([[call("step", {}, "c2")], [call("step", {}, "c3")], "unreachable"]);
  const resumed = new Session(new Agent({ model: "agent-model", provider, tools }), { storage: dying.memory, sessionId: crashed.id, resume: true });
  await assert.rejects(resumed.resume(), { code: "limit_exceeded", limit: "steps" });
  assert.deepEqual(provider.requests.map((request) => [request.model, request.instructions, request.extra, request.tools.some((offered) => offered.name === "final_output")]),
    [["run-model", "Run instructions.", { temperature: 0 }, true], ["run-model", "Run instructions.", { temperature: 0 }, true]]);
});

test("G19 replay of a reconciled run pairs every tool start with an end", async () => {
  const dying = dyingAt("tool_result");
  const tools = [tool("effect", () => "done")];
  const crashed = new Session(new Agent({ model: "m", provider: new ScriptedProvider([[call("effect")]]), tools }), { storage: dying });
  await assert.rejects(crashed.run("go"), StorageError);
  const session = new Session(new Agent({ model: "m", provider: new ScriptedProvider([]), tools }),
    { storage: dying.memory, sessionId: crashed.id, resume: true, onUncertain: "reconcile" });
  assert.equal(session.recoveryRequired, true);
  await session.reconcile("Checked: it happened.");
  const replay = session.events().events.map((event) => event.type);
  assert.deepEqual(replay, ["agent_start", "tool_execution_start", "tool_execution_end", "agent_end"]);
  assert.deepEqual(session.records.slice(-2).map((record) => [record.type, record.interrupted]), [["tool_result", true], ["reconciled", undefined]]);
});

test("G33 typed output survives a crash between the final_output call and the commit", async () => {
  const dying = dyingAt("run_commit");
  const crashed = new Session(new Agent({ model: "m", provider: new ScriptedProvider([[call("final_output", { answer: 7 })]]) }), { storage: dying });
  await assert.rejects(crashed.run("go", { outputSchema: schema }), StorageError);
  const provider = new ScriptedProvider([]);
  const session = new Session(new Agent({ model: "m", provider }), { storage: dying.memory, sessionId: crashed.id, resume: true });
  assert.equal(session.resumable, true);
  const result = await session.resume();
  assert.deepEqual([result.value, provider.requests.length, session.records.at(-1)?.value], [{ answer: 7 }, 0, { answer: 7 }]);
  assert.deepEqual(session.history.map((item) => item.role), ["user", "assistant", "tool_result"]);
});

test("G35 the library module is not a program: running it starts nothing and it never asks what the program is", () => {
  const library = fileURLToPath(new URL("./sdk.ts", import.meta.url));
  const source = readFileSync(library, "utf8");
  assert.doesNotMatch(source, /process\.argv|realpathSync|import\.meta\.url/);
  const run = spawnSync(process.execPath, [library, "--self-test"], { encoding: "utf8" });
  assert.deepEqual([run.status, run.stdout], [0, ""]);
  const entry = spawnSync(process.execPath, [fileURLToPath(new URL("./tiny_agent.ts", import.meta.url)), "--self-test"], { encoding: "utf8" });
  assert.deepEqual([entry.status, entry.stdout], [0, "self-test passed\n"]);
});
