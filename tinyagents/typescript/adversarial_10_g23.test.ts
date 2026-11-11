// Invariant 5: a handler that re-enters its session must not hang it. close() called from a tool
// handler waits for the run's generator, which waits for the handler: neither ever returns.
import test from "node:test";
import assert from "node:assert/strict";
import { Agent, Session, Tool, ScriptedProvider } from "./tiny_agent.ts";

test("G23 session.close() awaited inside a tool handler does not deadlock the run", async () => {
  let session: Session | undefined;
  const hangup = new Tool({ name: "hangup", description: "end the conversation", parameters: { type: "object" },
    handler: async () => { await session?.close(); return "closed"; } });
  session = new Session(new Agent({ model: "m", tools: [hangup],
    provider: new ScriptedProvider([[{ callId: "c1", name: "hangup", arguments: {} }], "bye"]) }));
  const outcome = await Promise.race([
    session.run("hang up").then(() => "settled", () => "settled"),
    new Promise<string>((resolve) => setTimeout(() => resolve("deadlocked"), 3000)),
  ]);
  assert.equal(outcome, "settled");
});
