// Invariant 4: replay must agree with the live event and the journal. A tool result made of
// content parts (E7) is replayed as the string "[object Object],[object Object]".
import test from "node:test";
import assert from "node:assert/strict";
import { Agent, Session, Tool, ScriptedProvider, type AgentEvent } from "./tiny_agent.ts";

test("G26 events() replays a content-part tool result as the parts, not as a coerced string", async () => {
  const parts = [{ type: "text", text: "the screenshot" }, { type: "image", url: "https://example.invalid/shot.png" }];
  const agent = new Agent({ model: "m", provider: new ScriptedProvider([[{ callId: "c1", name: "shot", arguments: {} }], "done"]),
    tools: [new Tool({ name: "shot", description: "shot", parameters: { type: "object" }, handler: () => parts })] });
  const session = new Session(agent);
  const live: AgentEvent[] = [];
  await session.run("go", { onEvent: (event) => { live.push(event); } });
  const end = (events: AgentEvent[]) => events.find((event) => event.type === "tool_execution_end");
  const liveEnd = end(live);
  const replayed = end(session.events().events);
  assert.ok(liveEnd?.type === "tool_execution_end" && replayed?.type === "tool_execution_end");
  assert.deepEqual(liveEnd.output, parts);
  assert.deepEqual(replayed.output, liveEnd.output);
});
