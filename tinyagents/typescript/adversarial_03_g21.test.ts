// F18: replay of a run that paused contains one agent_end with status "paused" per pause.
import test from "node:test";
import assert from "node:assert/strict";
import { Agent, Session, Tool, ScriptedProvider, pause, type AgentEvent } from "./tiny_agent.ts";

test("G21 events() replays the paused agent_end at the cursor the live event carried", async () => {
  const agent = new Agent({ model: "m", provider: new ScriptedProvider([[{ callId: "c1", name: "ask", arguments: {} }], "done"]),
    tools: [new Tool({ name: "ask", description: "ask", parameters: { type: "object" }, handler: () => pause({ question: "ok?" }) })] });
  const session = new Session(agent);
  const live: AgentEvent[] = [];
  await session.run("go", { onEvent: (event) => { live.push(event); } });
  const liveEnd = live.at(-1);
  assert.ok(liveEnd?.type === "agent_end" && liveEnd.status === "paused");
  const paused = (events: AgentEvent[]) => events.filter((event) => event.type === "agent_end" && event.status === "paused");
  assert.deepEqual(paused(session.events().events).map((event) => event.cursor), [liveEnd.cursor], "while paused");
  await session.resume({ result: "yes" });
  const after = session.events().events;
  assert.equal(paused(after).length, 1, "after the run resumed and completed");
  assert.equal(after.filter((event) => event.type === "agent_end").length, 2);
});
