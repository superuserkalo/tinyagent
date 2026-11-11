// F14: session.ready resolves when an asynchronous store has loaded. RpcSession does not wait
// for it: commands that arrive before the load resolves are answered from the empty,
// not-yet-loaded session as if that were the stored state.
import test from "node:test";
import assert from "node:assert/strict";
import { Agent, Session, RpcSession, ScriptedProvider, MemoryStorage, type Item, type Storage } from "./tiny_agent.ts";

test("G31 RPC commands issued before an async store has loaded see the stored session", async () => {
  const memory = new MemoryStorage();
  const agent = () => new Agent({ model: "m", provider: new ScriptedProvider(["hello"]) });
  // A stored session with one committed exchange and one interrupted run.
  const stored = new Session(agent(), { storage: memory });
  await stored.run("hi");
  const open = stored.stream("second");
  await open.next();   // run_start is journaled; the process "dies" here
  const storage: Storage = {
    load: async (id) => { await new Promise((resolve) => setTimeout(resolve, 30)); return memory.load(id); },
    append: async (id, expected, record) => memory.append(id, expected, record),
    list: async () => memory.list(),
    release: async () => {},
  };
  const output: Item[] = [];
  const rpc = new RpcSession(agent(), (value) => { output.push(value as Item); }, () => {},
    { storage, sessionId: stored.id, resume: true });
  await rpc.handle({ id: "history", type: "get_history" });
  await rpc.handle({ id: "state", type: "get_state" });
  await rpc.handle({ id: "abandon", type: "abandon_run" });
  await rpc.close();
  const response = (id: string) => output.find((line) => line.id === id) as { success: boolean; data?: Item; error?: Item };
  assert.equal((response("history").data?.history as Item[] | undefined)?.length, 2, "get_history returns the committed exchange");
  assert.equal(response("state").data?.resumable, true, "get_state reports the interrupted run");
  assert.equal(response("abandon").success, true, `abandon_run answered ${JSON.stringify(response("abandon").error)}`);
});
