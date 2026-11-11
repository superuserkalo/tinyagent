// The browser entry: nothing reachable from it needs Node, it bundles for the browser, and the
// bundle runs a durable session in a context that has only what a browser has. Also: journals
// written by version 0.2.0, before the Session's digests stopped using node:crypto, still open.
import assert from "node:assert/strict";
import { test, type TestContext } from "node:test";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { isBuiltin } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { Agent, Session, Tool, ScriptedProvider, OpenAIProvider, AnthropicProvider, ChatCompletionsProvider, type Item,
  type Provider, type ScriptedTurn } from "./tiny_agent.ts";
import type * as Browser from "./browser.ts";

/** What the bundle defines in the page: the browser entry's exports, as the global TinyAgent. */
type Page = { TinyAgent: typeof Browser; JOURNAL: string; journalAgent: typeof journalAgent };

const here = dirname(fileURLToPath(import.meta.url));
/** The sources in a checkout. Where only the installed package is, its compiled files. */
const fromSource = existsSync(join(here, "browser.ts"));
const entry = fromSource ? join(here, "browser.ts") : fileURLToPath(import.meta.resolve("tiny-agent-sdk/browser"));
const internal = (name: string) => fromSource ? `./${name}.ts` : `./node_modules/tiny-agent-sdk/dist/${name}.js`;

/** A journal written by version 0.2.0: three runs, a compaction, and a fourth run that crashed inside a tool call. */
const JOURNAL_V020 = String.raw`{"payload":"{\"type\":\"session\",\"sessionId\":\"s_journal_v020\",\"configuration\":\"35a819780bb55b85f5bb3779f1340a1e918b9a58a6bca531fa14037d1dcdc401\",\"version\":1,\"sequence\":1}","sha256":"b17d29350256d374408914d21563e302c7b4541a823a909410e6bb2ee52b8233"}
{"payload":"{\"type\":\"run_start\",\"runId\":\"r_fixed1\",\"item\":{\"role\":\"user\",\"content\":\"one 🎉\"},\"version\":1,\"sequence\":2}","sha256":"3b0c9eafc3ab7aa38ec9813225a562a07cac16d4ab38a44991121eb8b9c95365"}
{"payload":"{\"type\":\"item\",\"item\":{\"role\":\"assistant\",\"content\":[{\"type\":\"tool_call\",\"callId\":\"c1\",\"name\":\"echo\",\"arguments\":{\"text\":\"héllo 🎉\"}}]},\"toolCalls\":[{\"callId\":\"c1\",\"name\":\"echo\",\"arguments\":{\"text\":\"héllo 🎉\"}}],\"version\":1,\"sequence\":3}","sha256":"0254be56783feebdf69b4ad2039b0080fe0fc3c260a06be5e12f679abb1bdf1d"}
{"payload":"{\"type\":\"tool_intent\",\"callId\":\"c1\",\"toolName\":\"echo\",\"arguments\":{\"text\":\"héllo 🎉\"},\"idempotent\":false,\"version\":1,\"sequence\":4}","sha256":"fe77d82c74e0d5733cdad39c5ac85ece99db5cef8d3b78cae78499d6886fe4ce"}
{"payload":"{\"type\":\"tool_result\",\"item\":{\"role\":\"tool_result\",\"callId\":\"c1\",\"toolName\":\"echo\",\"output\":\"echo: héllo 🎉\",\"isError\":false},\"version\":1,\"sequence\":5}","sha256":"5d3c0625a9492fc35f07d595b8b6a78c4708d211c022acc0465c6a12cc5fd8e5"}
{"payload":"{\"type\":\"item\",\"item\":{\"role\":\"assistant\",\"content\":[{\"type\":\"text\",\"text\":\"first ☃\"}]},\"version\":1,\"sequence\":6}","sha256":"d52b4f6042ca2394b0db138c50804a67b78d1f78d969cb2c44503e72820beb4e"}
{"payload":"{\"type\":\"run_commit\",\"runId\":\"r_fixed1\",\"text\":\"first ☃\",\"usage\":{\"inputTokens\":0,\"outputTokens\":0,\"requests\":2},\"version\":1,\"sequence\":7}","sha256":"0229added37abcedc5c82f23a986ac4a0c6573124f45408f3810cf0dff7f9fce"}
{"payload":"{\"type\":\"run_start\",\"runId\":\"r_fixed2\",\"item\":{\"role\":\"user\",\"content\":\"two\"},\"version\":1,\"sequence\":8}","sha256":"8debac463067bdf3ca1517c19d02e69deee582cb903e71939c84ad72cb8599d6"}
{"payload":"{\"type\":\"item\",\"item\":{\"role\":\"assistant\",\"content\":[{\"type\":\"text\",\"text\":\"second\"}]},\"version\":1,\"sequence\":9}","sha256":"f22d0040102b764686aac132e69a8a8589a7983a59daa5276602f3faa776dda2"}
{"payload":"{\"type\":\"run_commit\",\"runId\":\"r_fixed2\",\"text\":\"second\",\"usage\":{\"inputTokens\":0,\"outputTokens\":0,\"requests\":1},\"version\":1,\"sequence\":10}","sha256":"14319e7a5a033c69c917624e6854e09a785cb673beb6c1bd70cb46eff88935af"}
{"payload":"{\"type\":\"checkpoint\",\"covered\":4,\"summary\":\"summary of the early talk\",\"historyHash\":\"800b5231533c57fe1e7ae3c05f50287bd4777b22b0a87c0e666fd98b8f526e1e\",\"version\":1,\"sequence\":11}","sha256":"841ab64d87eee44bf6c66388ade2867f129ac54f0df83dfa55de3696c31ba500"}
{"payload":"{\"type\":\"run_start\",\"runId\":\"r_fixed4\",\"item\":{\"role\":\"user\",\"content\":\"three\"},\"version\":1,\"sequence\":12}","sha256":"58f7a02d85d4da083d99900630cb62039c42771230807c6cd8599f2c50496f17"}
{"payload":"{\"type\":\"item\",\"item\":{\"role\":\"assistant\",\"content\":[{\"type\":\"text\",\"text\":\"third\"}]},\"version\":1,\"sequence\":13}","sha256":"d51dc27ce6b364b59a0eeb16d5530657b2a8171d78aaf6818f41ea30d61924d6"}
{"payload":"{\"type\":\"run_commit\",\"runId\":\"r_fixed4\",\"text\":\"third\",\"usage\":{\"inputTokens\":0,\"outputTokens\":0,\"requests\":1},\"version\":1,\"sequence\":14}","sha256":"7b833fc5b34d6f1899947f9dcf610776472c98777306a759e04a2e9e1491ae72"}
{"payload":"{\"type\":\"run_start\",\"runId\":\"r_fixed5\",\"item\":{\"role\":\"user\",\"content\":\"four, crashes\"},\"version\":1,\"sequence\":15}","sha256":"4d52299ce990498c5f774e4e22d3eafa2e7e4ef5b30609a25e534bc1d5e3457f"}
{"payload":"{\"type\":\"item\",\"item\":{\"role\":\"assistant\",\"content\":[{\"type\":\"tool_call\",\"callId\":\"c2\",\"name\":\"echo\",\"arguments\":{\"text\":\"crash\"}}]},\"toolCalls\":[{\"callId\":\"c2\",\"name\":\"echo\",\"arguments\":{\"text\":\"crash\"}}],\"version\":1,\"sequence\":16}","sha256":"440e2a9ad0b490f9ebe674219a9171ab43e2ad6beeea5b981ea841287f7099d7"}
{"payload":"{\"type\":\"tool_intent\",\"callId\":\"c2\",\"toolName\":\"echo\",\"arguments\":{\"text\":\"crash\"},\"idempotent\":false,\"version\":1,\"sequence\":17}","sha256":"058fd68cd5e903326af817e47b68d156b6b98daf11694bbfdbdf1feaf68105ec"}`;

/** The agent that wrote that journal: the same model, instructions and tool, so its configuration digest matches. */
function journalAgent(turns: ScriptedTurn[], sdk: Pick<typeof Browser, "Agent" | "Tool" | "ScriptedProvider"> = { Agent, Tool, ScriptedProvider }) {
  const provider = new sdk.ScriptedProvider(turns);
  const echo = new sdk.Tool({ name: "echo", description: "Echo the text ✓ — naïve 🎉",
    parameters: { type: "object", properties: { text: { type: "string" } }, required: ["text"] },
    handler: ({ text }) => `echo: ${String(text)}` });
  const agent = new sdk.Agent({ model: "scripted", instructions: "Be brief. Ünïcödé ☃ 🎉 and a long line ".repeat(3),
    tools: [echo], provider });
  return { agent, provider };
}

function scratch(t: TestContext): string {
  const directory = mkdtempSync(join(tmpdir(), "ta-browser-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  return directory;
}

/** Source without comments, so that prose that names a Node API is not taken for a use of it. */
function code(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'\\])\/\/.*$/gm, "$1");
}

/** Every module reachable from `start` through relative imports, type-only imports included, with what each imports. */
function importGraph(start: string): Map<string, string[]> {
  const graph = new Map<string, string[]>();
  const pending = [start];
  while (pending.length) {
    const file = pending.pop()!;
    if (graph.has(file)) continue;
    const source = code(readFileSync(file, "utf8"));
    const specifiers = [...source.matchAll(/\b(?:import|export)\b[^"'`;]*?\bfrom\s*["']([^"']+)["']/g),
      ...source.matchAll(/\bimport\s*["']([^"']+)["']/g), ...source.matchAll(/\b(?:import|require)\s*\(\s*["']([^"']+)["']/g)]
      .map((match) => match[1]);
    graph.set(file, specifiers);
    for (const specifier of specifiers) if (specifier.startsWith(".")) pending.push(resolve(dirname(file), specifier));
  }
  return graph;
}

test("nothing reachable from the browser entry imports Node or uses its globals", () => {
  const graph = importGraph(entry);
  const names = [...graph.keys()].map((file) => file.slice(dirname(entry).length + 1)).sort();
  // The walk did reach the core: a broken walk must not pass for a clean one.
  for (const module of ["agent", "session", "storage", "types", "openai", "anthropic", "chat", "sse", "scripted", "sha256"])
    assert.ok(names.some((name) => name.startsWith(`${module}.`)), `${module} is reachable`);
  for (const module of ["sdk", "file_storage", "bash", "rpc", "http", "cli", "tiny_agent"])
    assert.ok(!names.some((name) => name.startsWith(`${module}.`)), `${module} is not reachable`);
  for (const [file, specifiers] of graph) {
    for (const specifier of specifiers)
      assert.ok(!specifier.startsWith("node:") && !isBuiltin(specifier), `${file} imports ${specifier}`);
    const source = code(readFileSync(file, "utf8"));
    // process is read only as globalThis.process, where it may be missing.
    for (const pattern of [/\bBuffer\b/, /(?<![.\w])process\s*\./, /\brequire\s*\(/, /\b__dirname\b/, /\bsetImmediate\b/])
      assert.ok(!pattern.test(source), `${file} uses ${pattern.source}`);
  }
});

/** esbuild is a development dependency: a project that only installed the package has none. */
async function bundle(t: TestContext, format: "esm" | "iife"): Promise<string | undefined> {
  const esbuild = await import("esbuild").catch(() => undefined);
  if (!esbuild) {
    t.skip("esbuild is not installed");
    return undefined;
  }
  const result = await esbuild.build({ entryPoints: [entry], bundle: true, platform: "browser", format,
    globalName: "TinyAgent", write: false, metafile: true, logLevel: "silent" });
  assert.deepEqual(result.errors, []);
  const inputs = Object.keys(result.metafile.inputs);
  assert.ok(inputs.every((input) => !input.startsWith("node:")), inputs.join(" "));
  return result.outputFiles[0].text;
}

test("the browser entry bundles for the browser platform with no Node import left in it", async (t) => {
  for (const format of ["esm", "iife"] as const) {
    const text = await bundle(t, format);
    if (text === undefined) return;
    assert.ok(text.includes("session has an active operation"), "the bundle holds the Session");
    assert.doesNotMatch(text, /require\(\s*["']node:|from\s*["']node:|import\(\s*["']node:/);
  }
});

/** The globals a browser page has, and nothing of Node's: no process, require, Buffer, global or setImmediate. */
function browserContext(): vm.Context {
  return vm.createContext({
    fetch, Request, Response, Headers, URL, URLSearchParams, crypto, setTimeout, clearTimeout, setInterval, clearInterval,
    queueMicrotask, AbortController, AbortSignal, DOMException, Event, EventTarget, TextEncoder, TextDecoder,
    ReadableStream, structuredClone, console, atob, btoa, performance,
  });
}

/**
 * Runs in the vm context against the bundle's global TinyAgent. A multi-step run with a tool on a
 * MemoryStorage session crashes inside its second tool call: that Session object is abandoned
 * mid-call, as a closed tab abandons it, and a new Session over the same storage resumes the run.
 */
const crashAndResume = async () => {
  const { Agent, Session, Tool, MemoryStorage, ScriptedProvider } = (globalThis as unknown as Page).TinyAgent;
  const leaks = ["process", "require", "Buffer", "global", "setImmediate", "module"].filter((name) => name in globalThis);
  const storage = new MemoryStorage();
  const calls: unknown[] = [];
  let crashing = true;
  let crashed = () => {};
  const reached = new Promise<void>((resolve) => { crashed = resolve; });
  const lookup = new Tool({ name: "lookup", description: "Look a key up", idempotent: true,
    parameters: { type: "object", properties: { key: { type: "string" } }, required: ["key"] },
    handler: ({ key }) => {
      calls.push(key);
      if (key === "b" && crashing) {
        crashing = false;
        crashed();
        return new Promise(() => {});
      }
      return `value of ${key}`;
    } });
  const agent = (turns: ScriptedTurn[]) => new Agent({ model: "scripted", tools: [lookup], provider: new ScriptedProvider(turns) });
  const first = new Session(agent([[{ callId: "c1", name: "lookup", arguments: { key: "a" } }],
    [{ callId: "c2", name: "lookup", arguments: { key: "b" } }], "never reached"]), { storage });
  first.run("look up a, then b");
  await reached;
  // The crash: `first` is never used again. A new Session reads the same storage.
  const provider = new ScriptedProvider(["a and b are known"]);
  const second = new Session(new Agent({ model: "scripted", tools: [lookup], provider }),
    { storage, sessionId: first.id, resume: true });
  const resumable = second.resumable;
  const events: string[] = [];
  const result = await second.resume({ onEvent: (event) => { events.push(event.type); } });
  const after = await second.run("and now?").catch((error: Error) => error.message);
  let directory = "accepted";
  try { new Session(agent([]), { directory: "sessions" }); } catch (error) { directory = (error as Error).message; }
  return { leaks, resumable, calls, text: result.text, events, after, directory,
    results: result.history.filter((item) => item.role === "tool_result").map((item) => item.output),
    records: second.records.map((record) => record.type),
    resent: provider.requests[0].history.map((item) => item.role) };
};

/** close() called from a tool handler fails at once, without AsyncLocalStorage to say where the call came from. */
const closeFromHandler = async () => {
  const { Agent, Session, Tool, ScriptedProvider } = (globalThis as unknown as Page).TinyAgent;
  let session: Browser.Session | undefined;
  const hangup = new Tool({ name: "hangup", description: "end the conversation", parameters: { type: "object" },
    handler: async () => {
      try { await session?.close(); return "closed"; } catch (error) { return `refused: ${(error as { code?: string }).code}`; }
    } });
  session = new Session(new Agent({ model: "m", tools: [hangup],
    provider: new ScriptedProvider([[{ callId: "c1", name: "hangup", arguments: {} }], "bye"]) }));
  const started = Date.now();
  const result = await session.run("hang up");
  return { text: result.text, output: result.history[2].output, milliseconds: Date.now() - started };
};

/** A journal from version 0.2.0, moved into a MemoryStorage, opens and resumes with the digests computed without node:crypto. */
const oldJournal = async () => {
  const { TinyAgent: { Agent, Session, Tool, MemoryStorage, ScriptedProvider }, JOURNAL: journal, journalAgent: agentFor } =
    globalThis as unknown as Page;
  const storage = new MemoryStorage();
  journal.split("\n").forEach((line, index) => storage.append("s_journal_v020", index, JSON.parse(JSON.parse(line).payload) as Item));
  const { agent, provider } = agentFor(["recovered"], { Agent, Tool, ScriptedProvider });
  const session = new Session(agent, { storage, sessionId: "s_journal_v020", resume: true });
  const opened = { records: session.records.length, notes: session.recoveryNotes.length, resumable: session.resumable };
  const result = await session.resume();
  return { opened, text: result.text, summary: provider.requests[0].history[0].content, records: session.records.length };
};

async function inBrowser(t: TestContext, scenario: string): Promise<unknown> {
  const bundled = await bundle(t, "iife");
  if (bundled === undefined) return undefined;
  const context = browserContext();
  context.JOURNAL = JOURNAL_V020;
  vm.runInContext(`${bundled}\nglobalThis.TinyAgent = TinyAgent;`, context);
  vm.runInContext(`globalThis.journalAgent = ${journalAgent.toString()};`, context);
  // Results cross back as JSON, so that they compare as plain values from this realm.
  return JSON.parse(await vm.runInContext(`(${scenario})().then((value) => JSON.stringify(value))`, context));
}

test("the bundle runs a crash and a resume over MemoryStorage with browser globals only", async (t) => {
  const outcome = await inBrowser(t, crashAndResume.toString());
  if (outcome === undefined) return;
  assert.deepEqual(outcome, {
    leaks: [],
    resumable: true,
    // b was interrupted, and its tool is idempotent, so it ran again.
    calls: ["a", "b", "b"],
    text: "a and b are known",
    events: ["agent_start", "tool_execution_start", "tool_execution_end", "text_delta", "agent_end"],
    after: "ScriptedProvider has no turn for request 2",
    directory: "the directory option needs FileStorage, which runs only in Node; pass storage instead",
    results: ["value of a", "value of b"],
    records: ["session", "run_start", "item", "tool_intent", "tool_result", "item", "tool_intent", "tool_rerun",
      "tool_result", "item", "run_commit", "run_start", "run_failed"],
    resent: ["user", "assistant", "tool_result", "assistant", "tool_result"],
  });
});

test("in the bundle, close() from inside a tool handler fails at once with session_busy", async (t) => {
  const outcome = await inBrowser(t, closeFromHandler.toString()) as { text: string; output: string; milliseconds: number } | undefined;
  if (outcome === undefined) return;
  assert.equal(outcome.output, "refused: session_busy");
  assert.equal(outcome.text, "bye");
  assert.ok(outcome.milliseconds < 500, `${outcome.milliseconds} ms`);
});

test("in the bundle, a journal written by version 0.2.0 opens and resumes", async (t) => {
  const outcome = await inBrowser(t, oldJournal.toString());
  if (outcome === undefined) return;
  assert.deepEqual(outcome, {
    // 17 records and no more: the configuration digest matched, so no configuration record was added.
    opened: { records: 17, notes: 0, resumable: true },
    text: "recovered",
    // The checkpoint's history digest matched, so the compaction is in force.
    summary: "Earlier conversation summary:\nsummary of the early talk",
    records: 20,
  });
});

test("in Node, close() from a tool handler fails at once also after the handler has awaited", async () => {
  let session: Session | undefined;
  const hangup = new Tool({ name: "hangup", description: "end the conversation", parameters: { type: "object" },
    handler: async () => {
      await new Promise((resolve) => setTimeout(resolve, 5));
      try { await session?.close(); return "closed"; } catch (error) { return `refused: ${(error as { code?: string }).code}`; }
    } });
  session = new Session(new Agent({ model: "m", tools: [hangup],
    provider: new ScriptedProvider([[{ callId: "c1", name: "hangup", arguments: {} }], "bye"]) }));
  const result = await session.run("hang up");
  assert.equal(result.history[2].output, "refused: session_busy");
  await session.close();
});

test("a journal file written by version 0.2.0 opens, keeps its digests, and resumes", async (t) => {
  const directory = scratch(t);
  writeFileSync(join(directory, "s_journal_v020.jsonl"), `${JOURNAL_V020}\n`);
  const { agent, provider } = journalAgent(["recovered"]);
  const session = new Session(agent, { directory, sessionId: "s_journal_v020", resume: true });
  t.after(() => session.close());
  assert.equal(session.records.length, 17);
  assert.deepEqual(session.recoveryNotes, []);
  assert.equal(session.resumable, true);
  const result = await session.resume();
  assert.equal(result.text, "recovered");
  assert.equal(provider.requests[0].history[0].content, "Earlier conversation summary:\nsummary of the early talk");
  assert.deepEqual(session.records.slice(17).map((record) => record.type), ["tool_result", "item", "run_commit"]);
  assert.match(String((session.records[17].item as Item).output), /^This tool call was interrupted/);
  // What was there is unchanged, byte for byte; what was added has the same envelope.
  const written = readFileSync(join(directory, "s_journal_v020.jsonl"), "utf8");
  assert.ok(written.startsWith(`${JOURNAL_V020}\n`));
  for (const line of written.trimEnd().split("\n")) {
    const envelope = JSON.parse(line) as { payload: string; sha256: string };
    assert.equal(envelope.sha256, createHash("sha256").update(envelope.payload).digest("hex"));
  }
});

test("the portable SHA-256, byte length, random ID and sleep match what Node gives", async () => {
  const { sha256Hex } = await import(internal("sha256")) as { sha256Hex: (text: string) => string };
  const types = await import(internal("types")) as { utf8Length: (text: string) => number; randomId: () => string;
    sleep: (milliseconds: number, signal?: AbortSignal) => Promise<void> };
  const samples = ["", "abc", "naïve ☃ 🎉", "\ud800", "a\udc00b", "\udbff\udfff", "x".repeat(55), "x".repeat(56),
    "x".repeat(63), "x".repeat(64), "x".repeat(65), "é".repeat(10_000)];
  for (let index = 0; index < 200; index++)
    samples.push(String.fromCharCode(...Array.from({ length: index * 3 }, () => Math.floor(Math.random() * 0x10000))));
  for (const sample of samples) {
    assert.equal(sha256Hex(sample), createHash("sha256").update(sample).digest("hex"), JSON.stringify(sample));
    assert.equal(types.utf8Length(sample), Buffer.byteLength(sample, "utf8"), JSON.stringify(sample));
  }
  assert.match(types.randomId(), /^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$/);
  assert.notEqual(types.randomId(), types.randomId());
  const started = Date.now();
  await types.sleep(20);
  assert.ok(Date.now() - started >= 15);
  const controller = new AbortController();
  const waiting = types.sleep(60_000, controller.signal);
  controller.abort();
  await assert.rejects(waiting, { name: "AbortError" });
  await assert.rejects(types.sleep(1, AbortSignal.abort()), { name: "AbortError" });
});

test("providers send only headers a browser lets a page set, and Anthropic takes its browser-access header", async () => {
  // Headers a browser refuses to let a page set, or strips: the Fetch standard's forbidden request headers.
  const forbidden = /^(accept-charset|accept-encoding|access-control-request-.*|connection|content-length|cookie2?|date|dnt|expect|host|keep-alive|origin|referer|set-cookie|te|trailer|transfer-encoding|upgrade|user-agent|via|proxy-.*|sec-.*)$/i;
  const sent: Record<string, string>[] = [];
  const fetch = async (_url: string | URL | Request, init?: RequestInit) => {
    sent.push(init?.headers as Record<string, string>);
    return new Response("{}", { status: 400 });
  };
  const browserAccess = { "anthropic-dangerous-direct-browser-access": "true" };
  const providers: Provider[] = [new OpenAIProvider({ apiKey: "k", fetch }), new ChatCompletionsProvider({ apiKey: "k", fetch }),
    new AnthropicProvider({ apiKey: "k", fetch, headers: browserAccess })];
  for (const provider of providers)
    await assert.rejects(new Agent({ model: "m", provider }).run("hi"), /API error 400/);
  assert.equal(sent.length, 3);
  for (const headers of sent) for (const name of Object.keys(headers)) assert.doesNotMatch(name, forbidden);
  assert.equal(sent[2]["anthropic-dangerous-direct-browser-access"], "true");
  assert.equal(sent[2]["x-api-key"], "k");
});
