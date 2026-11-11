/** A fuzzer with a fixed seed. Cancels, closes and abandons runs over an asynchronous store at random moments, then checks the journal against what was seen. fuzz.test.ts runs it. */
// @ts-nocheck
import { Agent, Session, Tool, ScriptedProvider, MemoryStorage } from "./tiny_agent.ts";
const tool = (name: string, handler: any, extra: any = {}) => new Tool({ name, description: name, parameters: { type: "object" }, handler, ...extra });
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
let unhandled = 0;
process.on("unhandledRejection", (e) => { unhandled++; console.log("UNHANDLED", String(e)); });
let seed = 12345;
const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
const problems = new Map<string, number>();
const note = (p: string) => problems.set(p, (problems.get(p) ?? 0) + 1);

for (let round = 0; round < Number(process.env.TINYAGENT_FUZZ_ROUNDS ?? 150); round++) {
  const mem = new MemoryStorage();
  let inflight = 0, overlap = false;
  const storage = {
    load: async (id: string) => { await sleep(rnd() * 3); return mem.load(id); },
    append: async (id: string, e: number, r: any) => { inflight++; if (inflight > 1) overlap = true; await sleep(rnd() * 4); try { return mem.append(id, e, r); } finally { inflight--; } },
    list: async () => mem.list(), release: async () => { await sleep(1); },
  };
  let executions = 0;
  const provider = new ScriptedProvider([
    [{ callId: "a", name: "t", arguments: {} }, { callId: "b", name: "t", arguments: {} }], { text: "mid", deltas: ["m", "id"], toolCalls: [{ callId: "c", name: "t", arguments: {} }] }, "final", "x", "y", "z"]);
  const agent = new Agent({ model: "m", provider, tools: [tool("t", async (_a: any, signal: AbortSignal) => { executions++; await sleep(rnd() * 4); if (signal.aborted) throw new Error("stopped"); return "ok"; })] });
  const s = new Session(agent, { storage });
  const c = new AbortController();
  const events: any[] = [];
  const mode = Math.floor(rnd() * 4);
  const at = rnd() * 40;
  const run = s.run("go", { signal: c.signal, onEvent: async (e) => { events.push(e); if (mode === 3 && rnd() < 0.15) throw new Error("consumer broke"); } });
  let outcome = "ok";
  const killer = (async () => { await sleep(at); if (mode === 0) c.abort(); else if (mode === 1) await s.close(); })();
  let result: any;
  try { result = await run; } catch (e) { outcome = String(e); }
  await killer;
  // a second run and a concurrent call
  if (mode !== 1) { await Promise.allSettled([s.run("again"), s.run("again2"), s.abandon().catch(() => {})]); }
  await s.close();
  if (overlap) note("overlapping appends");
  const records = mem.load(s.id) ?? [];
  // journal invariants
  let open = false, intent: any; let terminals = 0, starts = 0;
  for (const r of records) {
    if (r.type === "run_start") { if (open) note("run_start inside open run"); open = true; starts++; }
    if (r.type === "tool_intent") { if (intent) note("intent while pending"); intent = r; }
    if (r.type === "tool_result") { if (!intent) note("result without intent"); intent = undefined; }
    if (r.type === "run_commit" || r.type === "run_failed") { if (!open) note("terminal outside run"); if (intent) note("terminal with pending intent"); open = false; terminals++; }
  }
  if (open) note("run left open mode" + mode);
  // event invariants (first run)
  const ends = events.filter((e) => e.type === "agent_end");
  if (ends.length > 1) note("two agent_end");
  const ts = events.filter((e) => e.type === "tool_execution_start").map((e) => e.toolCallId);
  const te = events.filter((e) => e.type === "tool_execution_end").map((e) => e.toolCallId);
  if (mode !== 3 && mode !== 1 && ts.join() !== te.join()) note(`start/end mismatch mode${mode} ${ts}|${te}`);
  // result vs journal
  const firstTerminal = records.find((r) => r.type === "run_commit" || r.type === "run_failed");
  if (result && firstTerminal?.type !== "run_commit") note("result returned but journal not committed");
  if (!result && firstTerminal?.type === "run_commit") note("rejected but journal committed mode" + mode + " " + outcome);
  const end = ends[0];
  if (end && firstTerminal && end.cursor !== firstTerminal.sequence) note(`agent_end cursor ${end.status} != terminal record (${firstTerminal.type}/${firstTerminal.status}) mode${mode}`);
  if (end && firstTerminal) { const js = firstTerminal.type === "run_commit" ? "completed" : firstTerminal.status; // A consumer that throws while handling the completed agent_end is the one case a direct stream may differ (G5).
    if (js !== end.status && !(mode === 3 && outcome.includes("consumer broke") && end.status === "completed")) note(`live ${end.status} vs journal ${js} mode${mode}`); }
  // reopen
  const s2 = new Session(new Agent({ model: "m", provider: new ScriptedProvider([]) }), { storage: mem, sessionId: s.id, resume: true });
  const replayEnds = s2.events({ limit: 1000 }).events.filter((e) => e.type === "agent_end").length;
  if (replayEnds !== terminals) note("replay ends != terminals");
}
console.log("unhandled", unhandled, Object.fromEntries(problems));
process.exit(0);
