// A provider whose stream ends without a turn (a custom provider that forgets its return value)
// makes the loop send the request again without bound: no retry cap, no maxSteps, no error.
import test from "node:test";
import assert from "node:assert/strict";
import { Agent, type Provider } from "./tiny_agent.ts";

test("G25 a provider that returns no turn fails the run instead of being asked again forever", async () => {
  let requests = 0;
  const provider = { async *stream() {
    requests += 1;
    if (requests > 25) throw new Error("test guard: the loop asked 25 times");
    yield { type: "text_delta", delta: "partial" };
  } } as unknown as Provider;
  const agent = new Agent({ model: "m", provider, maxSteps: 2 });
  await assert.rejects(agent.run("go"));
  assert.ok(requests <= 4, `the provider was asked ${requests} times with maxSteps 2 and a retry cap of 4`);
});
