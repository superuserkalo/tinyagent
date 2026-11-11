// Three smaller ones, each reachable with one wrong or unusual argument.
import test from "node:test";
import assert from "node:assert/strict";
import { Agent, Session, RpcSession, Tool, ScriptedProvider, type AgentEvent, type Item } from "./tiny_agent.ts";

test("G29 an invalid signal option does not leave the session busy for ever", async () => {
  const session = new Session(new Agent({ model: "m", provider: new ScriptedProvider(["one"]) }));
  await assert.rejects(session.run("x", { signal: {} as AbortSignal }));
  assert.equal((await session.run("y")).text, "one");
});

test("G29 a very long timeoutSeconds does not expire at once", async () => {
  const slow = new Tool({ name: "slow", description: "slow", parameters: { type: "object" }, timeoutSeconds: 30 * 24 * 3600,
    handler: () => new Promise((resolve) => setTimeout(() => resolve("finished"), 1500)) });
  const events: AgentEvent[] = [];
  await new Agent({ model: "m", tools: [slow], provider: new ScriptedProvider([[{ callId: "c1", name: "slow", arguments: {} }], "done"]) })
    .run("go", [], { onEvent: (event) => { events.push(event); } });
  const end = events.find((event) => event.type === "tool_execution_end");
  assert.ok(end?.type === "tool_execution_end");
  assert.deepEqual([end.isError, end.output], [false, "finished"]);
});

test("G29 one deeply nested prompt part does not put the session out of service", async () => {
  const output: Item[] = [];
  const rpc = new RpcSession(new Agent({ model: "m", provider: new ScriptedProvider(["one", "two"]) }),
    (value) => { output.push(value as Item); }, () => {});
  const part = JSON.parse(`{"type":"text","text":"x","junk":${"[".repeat(100_000)}${"]".repeat(100_000)}}`) as Item;
  await rpc.handle({ id: "deep", type: "prompt", message: [part] }).catch(() => {});
  await rpc.waitIdle();
  await rpc.handle({ id: "next", type: "prompt", message: "hello" });
  await rpc.waitIdle();
  await rpc.close();
  const next = output.find((line) => line.id === "next") as { success: boolean; error?: Item };
  assert.equal(next.success, true, JSON.stringify(next.error));
});
