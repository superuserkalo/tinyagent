// F21 with an asynchronous Storage: the terminal agent_end is delivered before the run's last
// record is appended, and the session is released only after that append resolves. A client
// that reacts to the agent_end line finds the session still running for as long as the store
// takes: one append for a completed run, up to two (tool_result, run_failed) otherwise.
import test from "node:test";
import assert from "node:assert/strict";
import { Agent, RpcSession, ScriptedProvider, MemoryStorage, type Item, type Storage } from "./tiny_agent.ts";

function slowStorage(milliseconds: number): Storage {
  const memory = new MemoryStorage();
  return {
    load: async (id) => memory.load(id),
    append: async (id, expected, record) => {
      await new Promise((resolve) => setTimeout(resolve, milliseconds));
      return memory.append(id, expected, record);
    },
    list: async () => memory.list(),
    release: async () => {},
  };
}

test("G18 after the agent_end line the session is idle and takes the next prompt (async storage)", async () => {
  const output: Item[] = [];
  const probes: Promise<void>[] = [];
  const agent = new Agent({ model: "m", provider: new ScriptedProvider(["one", "two"]) });
  const rpc: RpcSession = new RpcSession(agent, (value) => {
    const line = value as Item;
    output.push(line);
    // A client reads the line and answers on a later turn of the event loop, never inside the write.
    if (line.type === "agent_end" && !probes.length) probes.push(new Promise((resolve) => setImmediate(() => {
      void rpc.handle({ id: "state", type: "get_state" })
        .then(() => rpc.handle({ id: "next", type: "prompt", message: "again" })).then(resolve);
    })));
  }, () => {}, { storage: slowStorage(30) });
  await rpc.handle({ id: "first", type: "prompt", message: "hi" });
  await rpc.waitIdle();
  await Promise.all(probes);
  await rpc.waitIdle();
  const response = (id: string) => output.find((line) => line.id === id) as { success: boolean; data?: Item; error?: Item };
  await rpc.close();
  assert.equal(response("state").data?.status, "idle");
  assert.equal(response("next").success, true, JSON.stringify(response("next").error));
});
