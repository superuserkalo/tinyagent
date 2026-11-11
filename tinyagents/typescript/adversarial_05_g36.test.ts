// F2/F17: a live agent_end carries the cursor its terminal record will get. When a run is
// cancelled during a tool, the interrupted tool_result is written first, so run_failed lands
// one record later than the cursor the live agent_end announced.
import test from "node:test";
import assert from "node:assert/strict";
import { Agent, Session, Tool, ScriptedProvider, type AgentEvent } from "./tiny_agent.ts";

test("G36 agent_end of a run cancelled mid-tool carries the cursor of its run_failed record", async () => {
  const controller = new AbortController();
  const slow = new Tool({ name: "slow", description: "slow", parameters: { type: "object" },
    handler: (_arguments, signal) => new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(new Error("stopped")));
      setTimeout(() => controller.abort(), 10);
    }) });
  const agent = new Agent({ model: "m", tools: [slow], provider: new ScriptedProvider([[{ callId: "c1", name: "slow", arguments: {} }]]) });
  const session = new Session(agent);
  const live: AgentEvent[] = [];
  await assert.rejects(session.run("go", { signal: controller.signal, onEvent: (event) => { live.push(event); } }), { name: "AbortError" });
  const liveEnd = live.at(-1);
  assert.ok(liveEnd?.type === "agent_end" && liveEnd.status === "aborted");
  const record = session.records.find((item) => item.type === "run_failed");
  const replayed = session.events().events.find((event) => event.type === "agent_end");
  assert.equal(liveEnd.cursor, record?.sequence, "live cursor names the run_failed record");
  assert.equal(liveEnd.cursor, replayed?.cursor, "a follower can dedupe the live agent_end against replay");
});
