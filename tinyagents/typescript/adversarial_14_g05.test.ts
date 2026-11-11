// F23: a follower of an active run receives that run's agent_end before its stream closes.
// When the connection that owns a non-detached run drops during a tool, the run is cancelled
// (correct) but the follower's stream is closed after tool_execution_end, without agent_end.
import test from "node:test";
import assert from "node:assert/strict";
import { Agent, Tool, ScriptedProvider, serve } from "./tiny_agent.ts";

test("G05 a follower gets agent_end when the owning connection disconnects mid-tool", async (t) => {
  let toolStarted = () => {};
  const started = new Promise<void>((resolve) => { toolStarted = resolve; });
  const slow = new Tool({ name: "slow", description: "slow", parameters: { type: "object" },
    handler: (_arguments, signal) => new Promise((_resolve, reject) => {
      toolStarted();
      signal.addEventListener("abort", () => reject(new Error("stopped")));
    }) });
  const agent = new Agent({ model: "m", tools: [slow], provider: new ScriptedProvider([[{ callId: "c1", name: "slow", arguments: {} }]]) });
  const server = await serve(agent, { port: 0 });
  t.after(() => server.close());
  const headers = { "content-type": "application/json" };
  const { sessionId } = await (await fetch(`${server.url}/sessions`, { method: "POST" })).json() as { sessionId: string };
  const owner = new AbortController();
  void fetch(`${server.url}/sessions/${sessionId}/prompts`, { method: "POST", headers, body: JSON.stringify({ message: "go" }), signal: owner.signal })
    .then((response) => response.text()).catch(() => {});
  await started;
  const follow = await fetch(`${server.url}/sessions/${sessionId}/events?after=0&follow=1`);
  const reader = follow.body!.getReader();
  const decoder = new TextDecoder();
  let text = decoder.decode((await reader.read()).value);   // the replayed part has arrived: the follower is subscribed
  owner.abort();
  for (let chunk = await reader.read(); !chunk.done; chunk = await reader.read()) text += decoder.decode(chunk.value);
  const events = text.trim().split("\n").map((line) => JSON.parse(line) as { type: string; status?: string });
  assert.deepEqual([events.at(-1)?.type, events.at(-1)?.status], ["agent_end", "aborted"],
    `the follower received: ${events.map((event) => event.type).join(", ")}`);
});
