/** A fuzzer with a fixed seed. Kills the store at random records, reopens and resumes until the run finishes, then checks history and tool executions. fuzz.test.ts runs it. */
// @ts-nocheck
import { Agent, Session, Tool, MemoryStorage, pause } from "./tiny_agent.ts";
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
let unhandled = 0;
process.on("unhandledRejection", (e) => { unhandled++; console.log("UNHANDLED", String(e)); });
let seed = 777;
const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
const problems = new Map<string, number>();
const note = (p: string) => problems.set(p, (problems.get(p) ?? 0) + 1);
class Dead extends Error {}

function validate(history: any[], where: string) {
  // every assistant tool_call is followed (before the next non-tool_result item) by exactly one result; no orphan results
  let pending: string[] = [];
  for (const item of history) {
    if (item.role === "tool_result") {
      const i = pending.indexOf(item.callId);
      if (i < 0) note(`${where}: orphan/duplicate tool_result`); else pending.splice(i, 1);
    } else {
      if (pending.length) note(`${where}: call without result before ${item.role}`);
      pending = item.role === "assistant" && Array.isArray(item.content) ? item.content.filter((b: any) => b.type === "tool_call").map((b: any) => b.callId) : [];
    }
  }
  if (pending.length) note(`${where}: trailing call without result`);
}

for (let round = 0; round < Number(process.env.TINYAGENT_FUZZ_ROUNDS ?? 150); round++) {
  const mem = new MemoryStorage();
  const executions = new Map<string, number>();
  const resultsRecorded = new Set<string>();
  let turnNo = 0;
  // provider derives its turn from the history length so it is consistent across crashes
  const provider = { async *stream(request: any): any {
    validate(request.history, "request");
    const n = request.history.filter((i: any) => i.role === "assistant").length;
    turnNo++;
    if (turnNo > 60) throw new Error("runaway");
    const id = (k: string) => `${k}${n}`;
    const mk = (calls: any[]) => ({ text: "", assistant: { role: "assistant", content: calls.map((c) => ({ type: "tool_call", ...c })) }, output: [], toolCalls: calls });
    if (n === 0) return mk([{ callId: id("a"), name: "plain", arguments: {} }, { callId: id("b"), name: "idem", arguments: {} }]);
    if (n === 1) return mk([{ callId: id("p"), name: "ask", arguments: {} }, { callId: id("c"), name: "plain", arguments: {} }]);
    if (n === 2) return mk([{ callId: id("d"), name: "nope", arguments: {} }]);
    yield { type: "text_delta", delta: "fin" };
    return { text: "fin", assistant: { role: "assistant", content: [{ type: "text", text: "fin" }] }, output: [], toolCalls: [] };
  } };
  const exec = (ctx: any, idem: boolean) => { const k = ctx.executionId + (idem ? ":idem" : ""); executions.set(k, (executions.get(k) ?? 0) + 1); };
  const tools = [
    new Tool({ name: "plain", description: "", parameters: { type: "object" }, handler: async (_a, _s, ctx) => { exec(ctx, false); await sleep(0); return "ok"; } }),
    new Tool({ name: "idem", description: "", parameters: { type: "object" }, idempotent: true, handler: async (_a, _s, ctx) => { exec(ctx, true); return "ok"; } }),
    new Tool({ name: "ask", description: "", parameters: { type: "object" }, handler: (_a, _s, ctx) => { exec(ctx, false); return pause({ q: 1 }); } }),
  ];
  const agent = new Agent({ model: "m", provider: provider as any, tools });
  let done = false, lives = 0, first = true;
  const sid = "s_fuzz" + round;
  while (!done && lives++ < 40) {
    let budget = Math.floor(rnd() * 9) + 1; let dead = false;
    const before = rnd() < 0.5;
    const asyncStore = rnd() < 0.5;
    const doAppend = (id: string, e: number, r: any) => {
      if (dead) throw new Dead("dead");
      if (--budget <= 0) { dead = true; if (!before) { mem.append(id, e, r); } throw new Dead("crash"); }
      const seq = mem.append(id, e, r);
      if (r.type === "tool_result") resultsRecorded.add(r.item.callId);
      return seq;
    };
    const storage: any = asyncStore
      ? { load: async (id: string) => mem.load(id), append: async (id: string, e: number, r: any) => { await sleep(0); return doAppend(id, e, r); }, list: async () => mem.list(), release: async () => {} }
      : { load: (id: string) => mem.load(id), append: doAppend, list: () => mem.list(), release: () => {} };
    let s: Session;
    try {
      s = new Session(agent, first ? { storage, sessionId: sid } : { storage, sessionId: sid, resume: true });
      await s.ready;
    } catch (e) { if (!(e instanceof Dead) && !(e?.cause instanceof Dead)) note("open error: " + String(e).slice(0, 80)); if (first && !mem.load(sid)) continue; first = false; continue; }
    first = false;
    try {
      let r: any;
      if (s.paused) r = rnd() < 0.5 ? await s.resume({ result: "answer" }) : await s.resume({ error: "no" });
      else if (s.resumable) r = rnd() < 0.15 ? (await s.abandon(), await s.run("go")) : await s.resume();
      else r = await s.run("go");
      if (r.pause) { if (!s.paused) note("paused result but session.paused empty"); continue; }
      done = true;
      validate(s.history, "committed");
      const s3 = new Session(agent, { storage: mem, sessionId: sid, resume: true });
      if (JSON.stringify(s3.history) !== JSON.stringify(s.history)) note("reopened history differs");
      if (s3.resumable) note("resumable after commit");
    } catch (e) {
      if (!(e instanceof Dead) && !/dead|crash|closed or failed/.test(String(e))) note("run error: " + String(e).slice(0, 100));
    }
  }
  if (!done) note("never finished");
  for (const [k, n] of executions) if (!k.endsWith(":idem") && n > 1) note("non-idempotent executed twice");
}
console.log("unhandled", unhandled, Object.fromEntries(problems));
process.exit(0);
