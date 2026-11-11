// Invariant 1: a tool_execution_start must get exactly one tool_execution_end, also when the
// journal write of the tool_result (or of the tool_paused record) fails.
import test from "node:test";
import assert from "node:assert/strict";
import { Agent, Session, Tool, ScriptedProvider, MemoryStorage, pause, type AgentEvent, type Item } from "./tiny_agent.ts";

class FailsOn extends MemoryStorage {
  private readonly type: string;
  constructor(type: string) { super(); this.type = type; }
  override append(id: string, expected: number, record: Item): number {
    if (record.type === this.type) throw new Error("ENOSPC: no space left on device");
    return super.append(id, expected, record);
  }
}
const tool = (handler: () => unknown) => new Tool({ name: "t", description: "t", parameters: { type: "object" }, handler });

for (const [type, handler] of [["tool_result", () => "ok"], ["tool_paused", () => pause("why")]] as const) {
  test(`G01 storage error on ${type}: the started tool call still gets its end event`, async () => {
    const agent = new Agent({ model: "m", tools: [tool(handler)],
      provider: new ScriptedProvider([[{ callId: "c1", name: "t", arguments: {} }], "done"]) });
    const session = new Session(agent, { storage: new FailsOn(type) });
    const events: AgentEvent[] = [];
    await session.run("go", { onEvent: (event) => { events.push(event); } }).catch(() => {});
    const types = events.map((event) => event.type);
    assert.equal(types.filter((name) => name === "tool_execution_start").length, 1);
    assert.equal(types.filter((name) => name === "tool_execution_end").length, 1,
      `events were ${types.join(", ")}`);
  });
}
