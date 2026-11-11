// A tool failure is a recoverable observation; only cancellation of the run stops it. A handler
// (or hook) that throws an AbortError of its own, as fetch does when the handler's private
// timeout fires, ends the whole run as "aborted" although nobody cancelled it.
import test from "node:test";
import assert from "node:assert/strict";
import { Agent, Tool, ScriptedProvider, type AgentEvent } from "./tiny_agent.ts";

test("G22 an AbortError thrown by a tool while the run is not cancelled is a tool error, not a cancelled run", async () => {
  const lookup = new Tool({ name: "lookup", description: "lookup", parameters: { type: "object" },
    handler: async () => {
      const own = new AbortController();
      own.abort();   // the handler's own timeout
      await fetch("http://127.0.0.1:9/", { signal: own.signal });   // rejects with an AbortError before connecting
    } });
  const provider = new ScriptedProvider([[{ callId: "c1", name: "lookup", arguments: {} }], "The lookup timed out."]);
  const events: AgentEvent[] = [];
  const result = await new Agent({ model: "m", provider, tools: [lookup] })
    .run("go", [], { onEvent: (event) => { events.push(event); } }).catch((error: unknown) => error);
  const end = events.at(-1);
  assert.ok(end?.type === "agent_end");
  assert.equal(end.status, "completed", `run ended ${end.status}: ${String(result)}`);
  assert.equal(provider.requests.length, 2, "the model saw the tool error and answered");
});
