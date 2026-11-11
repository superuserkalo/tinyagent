// Invariant 5: after a storage failure the session stops with a named error and says how to
// recover. Over HTTP the failed run is reported as "provider_error", and every later request
// answers 500 internal_error "Request failed" with retryable: true, so a client retries forever.
import test from "node:test";
import assert from "node:assert/strict";
import { Agent, ScriptedProvider, MemoryStorage, serve, type Item } from "./tiny_agent.ts";

class Flaky extends MemoryStorage {
  failing = false;
  override append(id: string, expected: number, record: Item): number {
    if (this.failing && record.type === "item") throw new Error("ENOSPC: no space left on device");
    return super.append(id, expected, record);
  }
}

test("G28 a session whose storage failed is reported as such, not as a retryable internal error", async (t) => {
  const storage = new Flaky();
  const server = await serve(new Agent({ model: "m", provider: new ScriptedProvider(["one", "two"]) }), { port: 0, storage });
  t.after(() => server.close());
  const post = (path: string, body?: object) => fetch(server.url + path, { method: "POST",
    headers: { "content-type": "application/json" }, body: body && JSON.stringify(body) });
  const { sessionId } = await (await post("/sessions")).json() as { sessionId: string };
  storage.failing = true;
  const lines = (await (await post(`/sessions/${sessionId}/prompts`, { message: "go" })).text()).trim().split("\n");
  const end = JSON.parse(lines.at(-1) ?? "{}") as { status: string; error: { code: string } };
  storage.failing = false;
  const next = await post(`/sessions/${sessionId}/prompts`, { message: "again" });
  const body = await next.json() as { error: { code: string; retryable: boolean } };
  const observed = { runErrorIsProviderError: end.error.code === "provider_error", nextStatus: next.status, nextRetryable: body.error.retryable };
  // A storage failure is not a provider failure, and retrying a dead session can never succeed.
  assert.deepEqual(observed, { runErrorIsProviderError: false, nextStatus: 409, nextRetryable: false },
    `run error ${end.error.code}; next request ${next.status} ${JSON.stringify(body)}`);
});
