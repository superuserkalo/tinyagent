// Invariant 6 and A5: cancelling (or timing out) the bash tool returns promptly with the output
// so far. When the shell has exited but a child in another process group still holds the pipes
// (job control, setsid, a daemon), the abort kills nothing and the call waits for that child.
import test from "node:test";
import assert from "node:assert/strict";
import { runBash } from "./tiny_agent.ts";

const command = "set -m; sleep 6 & echo started";   // job control puts the background job in its own group

test("G12 cancelling bash returns promptly although a background job holds the pipes", { skip: process.platform === "win32" }, async () => {
  const controller = new AbortController();
  const began = Date.now();
  setTimeout(() => controller.abort(), 300);
  await assert.rejects(runBash(command, controller.signal), { name: "AbortError" });
  const elapsed = Date.now() - began;
  assert.ok(elapsed < 3000, `cancellation took ${elapsed} ms`);
});

test("G12 a bash timeout reports the output captured so far in the same situation", { skip: process.platform === "win32" }, async () => {
  const error = await runBash(command, undefined, { timeoutSeconds: 1 }).catch((thrown: Error) => thrown);
  assert.ok(error instanceof Error);
  assert.match(error.message, /started[\s\S]*\[timed out after 1 s\]/, `got: ${error.message}`);
});
