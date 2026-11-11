// A8: the interactive CLI asks before each command. The command is written to the terminal raw,
// so a model can put control characters in it (ESC [2K and CR erase the line) and make the
// prompt show a different command from the one that runs when the user answers "y".
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough } from "node:stream";
import { runCli, ScriptedProvider } from "./tiny_agent.ts";

test("G13 the approval prompt does not pass terminal control characters from the command through", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "ta-adversarial-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  // A terminal shows this as "$ ls": everything before the erase-line sequence disappears.
  const command = "echo this-ran-instead #\u001b[2K\r$ ls";
  const provider = new ScriptedProvider([[{ callId: "c1", name: "bash", arguments: { command } }], "done"]);
  const stdin = new PassThrough();
  const stdout = new PassThrough();
  const stderr = new PassThrough();
  let shown = "";
  stderr.on("data", (chunk: Buffer) => {
    shown += chunk.toString();
    if (shown.includes("Run it?") && !stdin.writableEnded) stdin.end("n\n");
  });
  stdout.resume();
  stdin.write("list the files\n");
  await runCli({ argv: [], env: { TINYAGENT_SESSION_DIR: directory }, provider, stdin, stdout, stderr });
  const prompt = shown.slice(0, shown.indexOf("Run it?"));
  assert.ok(prompt.includes("this-ran-instead"), "the real command is shown");
  assert.doesNotMatch(prompt, /[\u0000-\u0008\u000b-\u001f\u007f]/, `prompt bytes: ${JSON.stringify(prompt)}`);
});
