// Over HTTP, an automatic compaction inside a prompt run is treated as the end of the response:
// the body is closed after compaction_end, the next write fails, and the run is recorded aborted.
import test from "node:test";
import assert from "node:assert/strict";
import { Agent, ScriptedProvider, serve, type ProviderRequest } from "./tiny_agent.ts";

test("G30 a prompt that triggers auto-compaction over HTTP still streams to agent_end and commits", async (t) => {
  const turn = (request: ProviderRequest) => request.instructions.startsWith("Summarize") ? "short summary" : "x".repeat(300);
  const agent = new Agent({ model: "m", provider: new ScriptedProvider(Array.from({ length: 20 }, () => turn)) });
  const budget = { maxTokens: 1400, responseReserve: 100, countTokens: (request: object) => JSON.stringify(request).length };
  const server = await serve(agent, { port: 0, budget });
  t.after(() => server.close());
  const post = (path: string, body?: object) => fetch(server.url + path, { method: "POST",
    headers: { "content-type": "application/json" }, body: body && JSON.stringify(body) });
  const { sessionId } = await (await post("/sessions")).json() as { sessionId: string };
  let compacted = false;
  for (let index = 0; index < 6 && !compacted; index++) {
    const lines = (await (await post(`/sessions/${sessionId}/prompts`, { message: `question ${index}` })).text())
      .trim().split("\n").map((line) => JSON.parse(line) as { type: string; status?: string });
    compacted = lines.some((line) => line.type === "compaction_end");
    assert.deepEqual([lines.at(-1)?.type, lines.at(-1)?.status], ["agent_end", "completed"],
      `prompt ${index} streamed: ${lines.map((line) => line.type).join(", ")}`);
  }
  assert.ok(compacted, "the scenario must reach an automatic compaction");
  const replay = await (await fetch(`${server.url}/sessions/${sessionId}/events?limit=1000`)).json() as { events: { type: string; status?: string }[] };
  assert.ok(replay.events.filter((event) => event.type === "agent_end").every((event) => event.status === "completed"));
});
