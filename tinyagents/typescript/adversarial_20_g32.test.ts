// With an asynchronous Storage a cancellation that arrives while the final assistant message is
// being appended is not noticed before the terminal event: the client gets agent_end "completed",
// then run() rejects with AbortError and the journal records the run as aborted. F17 allows that
// disagreement only when the consumer cancels while handling the agent_end itself.
import test from "node:test";
import assert from "node:assert/strict";
import { Agent, Session, ScriptedProvider, MemoryStorage, type AgentEvent, type Storage } from "./tiny_agent.ts";

test("G32 a run cancelled before its agent_end is emitted does not announce itself as completed", async () => {
  const memory = new MemoryStorage();
  const controller = new AbortController();
  const storage: Storage = {
    load: async (id) => memory.load(id),
    append: async (id, expected, record) => {
      if (record.type === "item") setTimeout(() => controller.abort(), 5);   // the client cancels during this write
      await new Promise((resolve) => setTimeout(resolve, 30));
      return memory.append(id, expected, record);
    },
    list: async () => memory.list(),
    release: async () => {},
  };
  const session = new Session(new Agent({ model: "m", provider: new ScriptedProvider(["hello"]) }), { storage });
  const events: AgentEvent[] = [];
  await session.run("hi", { signal: controller.signal, onEvent: (event) => { events.push(event); } }).catch(() => {});
  const end = events.at(-1);
  assert.ok(end?.type === "agent_end");
  const journal = session.records.find((record) => record.type === "run_commit" || record.type === "run_failed");
  const journaled = journal?.type === "run_commit" ? "completed" : journal?.status;
  assert.equal(end.status, journaled, `live agent_end said ${end.status}, the journal says ${String(journaled)}`);
});
