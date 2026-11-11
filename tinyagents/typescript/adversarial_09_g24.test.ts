// Invariants 5 and 6: cancellation wins within bounded time and close() completes. A beforeTool
// hook (the same holds for afterTool, a handler without timeoutSeconds, onEvent and a provider)
// that never settles is awaited without racing the run's signal.
import test from "node:test";
import assert from "node:assert/strict";
import { Agent, Session, Tool, ScriptedProvider } from "./tiny_agent.ts";

const within = <T>(work: Promise<T>, milliseconds: number): Promise<string> => Promise.race([
  work.then(() => "resolved", (error: unknown) => `rejected: ${String(error)}`),
  new Promise<string>((resolve) => setTimeout(() => resolve("still pending"), milliseconds)),
]);

test("G24 cancelling a run stuck in a hook that never settles ends the run, and close() returns", async () => {
  const agent = new Agent({ model: "m", provider: new ScriptedProvider([[{ callId: "c1", name: "t", arguments: {} }]]),
    tools: [new Tool({ name: "t", description: "t", parameters: { type: "object" }, handler: () => "ok" })],
    beforeTool: () => new Promise<undefined>(() => {}) });
  const session = new Session(agent);
  const controller = new AbortController();
  const run = session.run("go", { signal: controller.signal });
  setTimeout(() => controller.abort(), 50);
  assert.equal(await within(run, 3000), "rejected: AbortError: run aborted");
  assert.equal(await within(session.close(), 2000), "resolved");
});
