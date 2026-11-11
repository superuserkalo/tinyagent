/** Part H: the wire behaviour Python and TypeScript must share. Titles start with the ruling. */
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough } from "node:stream";
import { test } from "node:test";
import { HttpFixture, body, json, object } from "./http_fixture.ts";
import { Agent, MemoryStorage, OpenAIProvider, RpcSession, ScriptedProvider, Session, Tool, pause, runCli,
  type AgentEvent, type Item, type Provider, type ScriptedTurn, type Storage, type ToolResult } from "./tiny_agent.ts";

const call = (name: string, arguments_: Item = {}, callId = `call_${name}`) => ({ callId, name, arguments: arguments_ });
const tool = (name: string, handler: Tool["handler"]) => new Tool({ name, description: name, parameters: { type: "object" }, handler });
const asking = (script: ScriptedTurn[]) => new Agent({ model: "m", provider: new ScriptedProvider(script), tools: [tool("ask", () => pause("Which region?"))] });
const lines = (text: string) => text.trimEnd().split("\n").map((line) => object(JSON.parse(line)));
const json_ = { "Content-Type": "application/json" };

test("H01 a provider_error is retryable after 429, 5xx or a lost connection, and not after a 4xx", async () => {
  const failing = (respond: () => Response) => new Agent({ model: "m", sleep: async () => {},
    provider: new OpenAIProvider({ apiKey: "k", fetch: async () => respond() }) });
  const cases: [() => Response, boolean][] = [[() => new Response("busy", { status: 429 }), true], [() => new Response("down", { status: 503 }), true],
    [() => { throw new TypeError("fetch failed"); }, true], [() => new Response("no", { status: 401 }), false], [() => new Response("bad", { status: 400 }), false]];
  for (const [respond, retryable] of cases) {
    const events: AgentEvent[] = [];
    await failing(respond).run("go", [], { onEvent: (event) => { events.push(event); } }).catch(() => {});
    const end = events.at(-1);
    assert.ok(end?.type === "agent_end" && end.status === "failed");
    assert.deepEqual([end.error.code, end.error.retryable], ["provider_error", retryable]);
  }
  // The same flag reaches an RPC client and the journal's replay.
  const emitted: Item[] = [];
  const rpc = new RpcSession(failing(() => new Response("down", { status: 502 })), (value) => { emitted.push(value as Item); }, () => {});
  await rpc.handle({ id: "p", type: "prompt", message: "go" });
  await rpc.waitIdle();
  assert.equal(object(emitted.at(-1)?.error).retryable, true);
  assert.equal(JSON.parse(JSON.stringify(rpc.session.events().events.at(-1))).error.retryable, true);
  await rpc.close();
});

test("H02 a deduplicated prompt's agent_start and agent_end carry the stored run's cursors", async () => {
  const session = new Session(new Agent({ model: "m", provider: new ScriptedProvider(["one", "two"]) }));
  await session.run("first");
  const live: AgentEvent[] = [];
  await session.run("question", { messageId: "m1", onEvent: (event) => { live.push(event); } });
  const repeated: AgentEvent[] = [];
  await session.run("question", { messageId: "m1", onEvent: (event) => { repeated.push(event); } });
  const record = (type: string) => [...session.records].reverse().find((saved) => saved.type === type)?.sequence;
  assert.deepEqual(repeated.map((event) => [event.type, event.cursor]), [["agent_start", record("run_start")], ["agent_end", record("run_commit")]]);
  assert.deepEqual(repeated.map((event) => event.cursor), [live[0].cursor, live.at(-1)?.cursor]);
});

test("H03 a storage failure before a stream starts is 409 session_failed over HTTP, never 500", async (t) => {
  const memory = new MemoryStorage();
  let broken = true;
  const storage: Storage = { load: (id) => memory.load(id), list: () => memory.list(), release: () => {},
    append(id, expected, record) { if (broken) throw new Error("ENOSPC"); return memory.append(id, expected, record); } };
  const fixture = new HttpFixture(new Agent({ model: "m", provider: new ScriptedProvider(["one"]) }), { storage });
  t.after(() => fixture.dispose());
  const refused = await fixture.send("POST", "/sessions");
  assert.deepEqual([refused.statusCode, object((await json(refused)).error)], [409, { code: "session_failed",
    message: "The session's storage failed. Open the session again: ENOSPC", retryable: false }]);
  broken = false;
  const id = await fixture.session();
  broken = true;
  const run = await fixture.prompt(id, "go");
  assert.deepEqual([run.statusCode, object((await json(run)).error).code], [409, "session_failed"]);
  broken = false;
  for (const [method, path] of [["POST", "prompts"], ["POST", "resume"], ["POST", "abandon"]]) {
    const next = await fixture.send(method, `/sessions/${id}/${path}`, JSON.stringify({ message: "again" }), json_);
    assert.deepEqual([next.statusCode, object((await json(next)).error).code], [409, "session_failed"], path);
  }
});

test("H04 H05 H06 refused input is invalid_command over RPC and invalid_request over HTTP, and changes nothing", async (t) => {
  const deep = JSON.parse(`[{"type":"text","text":"x","junk":${"[".repeat(40)}${"]".repeat(40)}}]`) as Item[];
  const emitted: Item[] = [];
  const rpc = new RpcSession(asking([[call("ask")], "thanks"]), (value) => { emitted.push(value as Item); }, () => {});
  const code = async (request: Item) => {
    await rpc.handle({ id: "x", ...request });
    await rpc.waitIdle();
    return object(emitted.at(-1)?.error).code;
  };
  assert.equal(await code({ type: "prompt", message: deep }), "invalid_command");
  await rpc.handle({ id: "p", type: "prompt", message: "go" });
  await rpc.waitIdle();
  const records = rpc.session.records.length;
  assert.equal(await code({ type: "resume_run", error: { not: "a string" } }), "invalid_command");
  assert.equal(await code({ type: "resume_run", result: "x".repeat(200_000) }), "invalid_command");
  assert.match(String(object(emitted.at(-1)?.error).message), /result exceeds 131072 UTF-8 bytes/);
  assert.equal(await code({ type: "resume_run" }), "invalid_command");
  assert.deepEqual([rpc.session.records.length, rpc.session.paused?.toolName], [records, "ask"]);
  await rpc.close();

  const fixture = new HttpFixture(asking([[call("ask")], "thanks"]));
  t.after(() => fixture.dispose());
  const id = await fixture.session();
  const post = async (path: string, value: unknown) => {
    const response = await fixture.send("POST", `/sessions/${id}/${path}`, JSON.stringify(value), json_);
    return [response.statusCode, response.statusCode === 200 ? await body(response) : object((await json(response)).error).code];
  };
  assert.deepEqual(await post("prompts", { message: deep }), [400, "invalid_request"]);
  assert.deepEqual(await post("prompts", { message: "go", detach: "yes" }), [400, "invalid_request"]);
  assert.deepEqual(await post("prompts", { message: "go", detach: 1 }), [400, "invalid_request"]);
  assert.equal((await post("prompts", { message: "go" }))[0], 200);
  assert.deepEqual(await post("resume", { error: 7 }), [400, "invalid_request"]);
  assert.deepEqual(await post("resume", { result: "x".repeat(200_000) }), [400, "invalid_request"]);
  assert.deepEqual(await post("resume", { result: "eu", detach: null }), [400, "invalid_request"]);
  assert.notEqual((await json(await fixture.send("GET", `/sessions/${id}`))).paused, null);
});

test("H07 an explicit compact while a run is open or paused is refused with resume_required", async (t) => {
  const budget = { maxTokens: 10_000, responseReserve: 100, countTokens: (request: Item) => JSON.stringify(request).length };
  const session = new Session(asking([[call("ask")]]), { budget });
  await session.run("go");
  await assert.rejects(session.compact(), { code: "resume_required" });
  const emitted: Item[] = [];
  const rpc = new RpcSession(asking([[call("ask")]]), (value) => { emitted.push(value as Item); }, () => {}, { budget });
  await rpc.handle({ id: "p", type: "prompt", message: "go" });
  await rpc.waitIdle();
  await rpc.handle({ id: "c", type: "compact" });
  await rpc.waitIdle();
  assert.equal(object(emitted.at(-1)?.error).code, "resume_required");
  await rpc.close();
  const fixture = new HttpFixture(asking([[call("ask")]]), { budget });
  t.after(() => fixture.dispose());
  const id = await fixture.session();
  await body(await fixture.prompt(id, "go"));
  const refused = await fixture.send("POST", `/sessions/${id}/compact`);
  assert.deepEqual([refused.statusCode, object((await json(refused)).error).code], [409, "resume_required"]);
});

test("H08 every agent_end carries usage: live, paused, replayed, and the ones an adapter makes up", async (t) => {
  const usage = (event: Item | AgentEvent | undefined) => JSON.parse(JSON.stringify(event)).usage as Item;
  const zero = { inputTokens: 0, outputTokens: 0, requests: 0 };
  const session = new Session(asking([{ toolCalls: [call("ask")], usage: { inputTokens: 5, outputTokens: 2 } }]));
  const live: AgentEvent[] = [];
  await session.run("go", { onEvent: (event) => { live.push(event); } });
  assert.deepEqual(usage(live.at(-1)), { inputTokens: 5, outputTokens: 2, requests: 1 });
  await session.abandon();
  await session.run("fails").catch(() => {});
  const ends = session.events().events.filter((event) => event.type === "agent_end");
  assert.deepEqual(ends.map((event) => [event.type === "agent_end" && event.status, usage(event)]),
    [["paused", { inputTokens: 5, outputTokens: 2, requests: 1 }], ["failed", zero], ["failed", zero]]);
  // The client goes away mid-run: the adapter ends the stream for a follower itself.
  let started = () => {};
  const waiting = new Promise<void>((resolve) => { started = resolve; });
  const provider: Provider = { async *stream(request) {
    yield { type: "text_delta", delta: "partial" };
    started();
    await new Promise((resolve) => request.signal.addEventListener("abort", resolve));
    throw new Error("stopped");
  } };
  const fixture = new HttpFixture(new Agent({ model: "m", provider }));
  t.after(() => fixture.dispose());
  const id = await fixture.session();
  const owner = await fixture.prompt(id, "go");
  await waiting;
  const follower = await fixture.send("GET", `/sessions/${id}/events?follow=1`);
  owner.destroy();
  const end = lines(await body(follower)).at(-1);
  assert.deepEqual([end?.type, end?.status, typeof usage(end).requests], ["agent_end", "aborted", "number"]);
});

test("H11 afterTool sees every call that ran, failed ones too, and not a denied, paused or interrupted one", async () => {
  const seen: [string, ToolResult][] = [];
  const provider = new ScriptedProvider([[call("ok"), call("boom"), call("missing"), call("denied"), call("ask")], "after the pause", "end"]);
  const agent = new Agent({ model: "m", provider,
    tools: [tool("ok", () => "fine"), tool("boom", () => { throw new Error("broke"); }), tool("denied", () => "never"), tool("ask", () => pause("?"))],
    beforeTool: (toolCall) => toolCall.name === "denied" ? { deny: "no" } : undefined,
    afterTool: (toolCall, result) => {
      seen.push([toolCall.name, result]);
      return toolCall.name === "boom" ? { output: "recovered by the hook", isError: false } : toolCall.name === "ok" ? "FINE" : undefined;
    } });
  const session = new Session(agent);
  await session.run("go");
  assert.deepEqual(seen, [["ok", { output: "fine", isError: false }], ["boom", { output: "Error: broke", isError: true }],
    ["missing", { output: "Unknown tool: missing", isError: true }]]);
  await session.resume({ result: "answered" });
  assert.deepEqual(seen.at(-1), ["ask", { output: "answered", isError: false }]);
  assert.deepEqual(provider.requests[1].history.slice(2).map((item) => [item.isError, item.output]),
    [[false, "FINE"], [false, "recovered by the hook"], [true, "Unknown tool: missing"], [true, "no"], [false, "answered"]]);
  // What it returns is checked: anything unusable, or too large, is the hook's error and ends the run.
  for (const returned of [42, { output: 7 }, { output: "x".repeat(200_000) }]) {
    const broken = new Session(new Agent({ model: "m", provider: new ScriptedProvider([[call("ok")], "never"]), tools: [tool("ok", () => "fine")],
      afterTool: () => returned as unknown as string }));
    await assert.rejects(broken.run("go"), { code: "hook_error" });
    assert.deepEqual(broken.records.slice(-2).map((record) => record.type), ["tool_result", "run_failed"]);
  }
});

test("H12 approval takes y/yes, n/no and a/all in any case, and /resume <text> answers a pause", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "ta-h-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const cli = async (argv: string[], typed: string, provider: ScriptedProvider) => {
    const stdin = new PassThrough();
    const out = { stdout: "", stderr: "" };
    const sink = (key: "stdout" | "stderr") => { const stream = new PassThrough(); stream.on("data", (chunk: Buffer) => { out[key] += chunk.toString(); }); return stream; };
    stdin.end(typed);
    const code = await runCli({ argv, env: { TINYAGENT_SESSION_DIR: directory }, provider, stdin, stdout: sink("stdout"), stderr: sink("stderr") });
    return { code, ...out, results: provider.requests.map((request) => request.history.at(-1)).filter((item) => item?.role === "tool_result").map((item) => String(item?.output)) };
  };
  const bash = (...commands: string[]): ScriptedTurn[] => [...commands.map((command, index) => [call("bash", { command }, `c${index}`)]), "finished"];
  const ran = (word: string) => `exit_code: 0\nstdout:\n${word}\nstderr:\n`;
  const answers = await cli([], "go\nYES\nNo\nno thanks\nY\nN\n", new ScriptedProvider(bash("echo one", "echo two", "echo three", "echo four", "echo five")));
  assert.deepEqual(answers.results, [ran("one"), "Rejected by user", "Rejected by user: no thanks", ran("four"), "Rejected by user"]);
  const all = await cli([], "go\nALL\n", new ScriptedProvider(bash("echo one", "echo two")));
  assert.deepEqual([all.results, all.stderr.split("Run it?").length - 1], [[ran("one"), ran("two")], 1]);
  // A paused run, made by another agent on the same session directory, is answered from the prompt.
  const paused = new Session(asking([[call("ask")]]), { directory });
  await paused.run("deploy");
  await paused.close();
  const provider = new ScriptedProvider(["Deploying to eu-west."]);
  const resumed = await cli(["--resume", paused.id], "/resume eu-west\n", provider);
  assert.equal(resumed.code, 0, resumed.stderr);
  assert.match(resumed.stdout, /paused: "Which region\?"[\s\S]*\/resume <answer>[\s\S]*Deploying to eu-west\./);
  assert.equal(provider.requests[0].history.at(-1)?.output, "eu-west");
});
