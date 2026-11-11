/** The two fuzzers from the adversarial review, with their fixed seeds and a round count that suits CI. */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

for (const [name, rounds] of [["fuzz_cancel.ts", "120"], ["fuzz_crash.ts", "120"]]) {
  test(`G18 G32 G36 ${name} finds no problem`, { timeout: 120_000 }, () => {
    const run = spawnSync(process.execPath, [fileURLToPath(new URL(`./${name}`, import.meta.url))],
      { encoding: "utf8", env: { ...process.env, TINYAGENT_FUZZ_ROUNDS: rounds } });
    assert.equal(run.status, 0, run.stderr);
    assert.equal(run.stdout.trim().split("\n").at(-1), "unhandled 0 {}", run.stdout.slice(-2000));
  });
}
