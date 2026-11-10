import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import type { Item } from "./tiny_agent.ts";

/** One scripted OpenAI Responses stream. */
function sse(...events: Item[]): string {
  return events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("");
}

function callForecast(callId: string, city: string): string {
  const call = { type: "function_call", id: `item_${callId}`, call_id: callId, name: "forecast",
    arguments: JSON.stringify({ city }) };
  return sse({ type: "response.completed", response: { output: [call] } });
}

function say(text: string): string {
  const message = { type: "message", id: "message", content: [{ type: "output_text", text }] };
  return sse({ type: "response.output_text.delta", delta: text },
    { type: "response.completed", response: { output: [message] } });
}

const summary = "Lisbon will be clear, 15 to 24 degrees. A Porto lookup was cancelled and may be repeated.";
const responses = [
  // First process: one complete run, then a run cancelled at its tool call.
  callForecast("call_1", "Lisbon"),
  say("Lisbon will be clear, 15 to 24 degrees."),
  callForecast("call_2", "Porto"),
  // Second process: the repeated question, the compaction summary, and one more run.
  callForecast("call_3", "Porto"),
  say("Porto will be clear too."),
  say(summary),
  say("Either city suits a walk."),
];

test("the session example cancels, restarts, resumes, compacts, and continues offline", { timeout: 30000 }, async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "tinyagent-example-"));
  t.after(() => rmSync(directory, { recursive: true }));
  const requests: Item[] = [];
  const server = createServer(async (request, response) => {
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(chunk as Buffer);
    requests.push(JSON.parse(Buffer.concat(chunks).toString("utf8")) as Item);
    const scripted = responses[requests.length - 1];
    response.writeHead(scripted ? 200 : 500, { "Content-Type": "text/event-stream" }).end(scripted);
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  t.after(() => new Promise((resolve) => { server.closeAllConnections(); server.close(resolve); }));
  const address = server.address();
  assert.ok(address && typeof address !== "string");

  // Each call is a separate operating-system process. They share only the session directory.
  const example = async (): Promise<string> => {
    const child = spawn(process.execPath, [fileURLToPath(new URL("./examples/session.ts", import.meta.url))], {
      env: { ...process.env, OPENAI_API_KEY: "offline", TINYAGENT_SESSION_DIR: directory,
        TINYAGENT_ENDPOINT: `http://127.0.0.1:${address.port}/v1/responses` },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk: Buffer) => { stdout += chunk.toString(); });
    child.stderr.on("data", (chunk: Buffer) => { stderr += chunk.toString(); });
    const code = await new Promise((resolve, reject) => {
      child.once("error", reject);
      child.once("close", resolve);
    });
    assert.equal(code, 0, stderr);
    return stdout;
  };

  const first = await example();
  assert.equal(requests.length, 3);
  const sessionId = /\[saved (s_[0-9a-f]+); run this again to resume it\]\n$/.exec(first)?.[1];
  assert.ok(sessionId, first);
  assert.equal(first,
    '[forecast {"city":"Lisbon"}]\nLisbon will be clear, 15 to 24 degrees.\n[run completed]\n' +
    `[forecast {"city":"Porto"}]\n\n[run aborted]\n[saved ${sessionId}; run this again to resume it]\n`);

  const second = await example();
  assert.equal(requests.length, 7);
  assert.equal(second,
    `[resumed ${sessionId} with 4 committed messages]\n[interrupted: forecast {"city":"Porto"}]\n` +
    '[forecast {"city":"Porto"}]\nPorto will be clear too.\n[run completed]\n' +
    "[compacted: true; 9 messages still in history]\n" +
    "Either city suits a walk.\n[run completed]\n");

  // The retry saw the committed Lisbon exchange and the reconciliation, not the cancelled call.
  const retried = JSON.stringify(requests[3].input);
  assert.match(retried, /call_1/);
  assert.match(retried, /Interrupted run recovery: The Porto forecast lookup was cancelled/);
  assert.doesNotMatch(retried, /call_2/);
  // Compaction is an ordinary completion without tools.
  assert.match(String(requests[5].instructions), /^Summarize/);
  assert.deepEqual(requests[5].tools, []);
  // The last run saw the summary in place of the Lisbon exchange, and the Porto exchange intact.
  const continued = JSON.stringify(requests[6].input);
  assert.ok(continued.includes(JSON.stringify(`Earlier conversation summary:\n${summary}`).slice(1, -1)), continued);
  assert.doesNotMatch(continued, /call_1/);
  assert.match(continued, /call_3/);
  assert.match(continued, /better day for a walk/);

  // On disk: every record of both processes, including the run that was cancelled.
  const journal = readFileSync(join(directory, `${sessionId}.jsonl`), "utf8").trimEnd().split("\n")
    .map((line) => (JSON.parse((JSON.parse(line) as { payload: string }).payload) as Item).type);
  const count = (type: string) => journal.filter((recorded) => recorded === type).length;
  assert.deepEqual([count("session"), count("run_commit"), count("run_failed"), count("reconciled"), count("checkpoint")],
    [1, 3, 1, 1, 1]);
});
