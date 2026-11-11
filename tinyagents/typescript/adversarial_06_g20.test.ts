// E4: beforeTool may return pause(payload); session.resume({ result }) supplies that tool's
// result and continues the run. The hook runs again on resume and its pause wins over the
// supplied result, so the run pauses again, forever.
import test from "node:test";
import assert from "node:assert/strict";
import { Agent, Session, Tool, ScriptedProvider, pause } from "./tiny_agent.ts";

test("G20 a run paused by a stateless beforeTool hook continues when resumed with a result", async () => {
  const provider = new ScriptedProvider([[{ callId: "c1", name: "deploy", arguments: {} }], "Deployed."]);
  const agent = new Agent({ model: "m", provider,
    tools: [new Tool({ name: "deploy", description: "deploy", parameters: { type: "object" }, handler: () => "deployed" })],
    beforeTool: (call) => call.name === "deploy" ? pause({ question: "Deploy to production?" }) : undefined });
  const session = new Session(agent);
  const first = await session.run("ship it");
  assert.ok(first.pause);
  const second = await session.resume({ result: "approved by Ana" });
  assert.equal(second.pause, undefined, "the supplied result settles the call");
  assert.equal(second.text, "Deployed.");
  assert.equal(session.records.filter((record) => record.type === "tool_paused").length, 1);
});
