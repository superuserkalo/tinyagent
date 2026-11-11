// The demo's claim, end to end and offline: a run whose server is SIGKILLed in
// the middle of a publish is resumed by a new server from the journal, every
// version exists once, and a client following by cursor sees nothing twice.
import assert from "node:assert/strict";
import { spawn, type ChildProcess } from "node:child_process";
import { once } from "node:events";
import { mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import test from "node:test";

type Event = Record<string, unknown> & { type: string; cursor?: number };

const children = new Set<ChildProcess>();

/** Start the demo server in scripted mode and wait until it says where it listens. */
/** `slow` is how long the scripted build holds its second publish open, in milliseconds. */
async function start(data: string, port: number, slow: number): Promise<{ child: ChildProcess; url: string }> {
  const child = spawn(process.execPath, [join(import.meta.dirname, "server.ts"), "--scripted", "--port", String(port), "--data", data], {
    stdio: ["ignore", "pipe", "inherit"],
    env: { ...process.env, HTML_BUILDER_SLOW_MS: String(slow), HTML_BUILDER_TYPING_MS: "0", NODE_NO_WARNINGS: "1" },
  });
  children.add(child);
  child.once("exit", () => children.delete(child));
  let output = "";
  for await (const chunk of child.stdout!) {
    output += String(chunk);
    const match = /http:\/\/127\.0\.0\.1:\d+/.exec(output);
    if (match) return { child, url: match[0] };
  }
  throw new Error(`the server exited before listening: ${output}`);
}

const post = (url: string, body?: object, headers: Record<string, string> = {}) =>
  fetch(url, { method: "POST", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body ?? {}) });

/** Read a JSONL stream into `into` until it ends or its server dies. */
async function collect(url: string, into: Event[]): Promise<void> {
  try {
    const response = await fetch(url);
    assert.equal(response.status, 200);
    const reader = response.body!.pipeThrough(new TextDecoderStream()).getReader();
    for (let buffer = ""; ;) {
      const { value, done } = await reader.read();
      if (done) return;
      buffer += value;
      for (let end; (end = buffer.indexOf("\n")) >= 0; buffer = buffer.slice(end + 1)) into.push(JSON.parse(buffer.slice(0, end)) as Event);
    }
  } catch (error) {
    if (error instanceof assert.AssertionError) throw error;   // anything else is the server dying under us, which is the point
  }
}

async function until<T>(what: string, check: () => T | Promise<T>, seconds = 15): Promise<NonNullable<T>> {
  for (const deadline = Date.now() + seconds * 1000; Date.now() < deadline; await sleep(25)) {
    const value = await check();
    if (value) return value;
  }
  throw new Error(`timed out waiting for ${what}`);
}

test("a run killed in the middle of a publish resumes and publishes every version once", { timeout: 90_000 }, async (t) => {
  const data = mkdtempSync(join(tmpdir(), "html-builder-test-"));
  t.after(async () => {
    for (const child of children) {
      child.kill("SIGKILL");
      await once(child, "exit");
    }
    rmSync(data, { recursive: true, force: true });
  });

  // ---- First server: start a detached run and follow it. ----
  const first = await start(data, 0, 60_000);   // far longer than the test: the publish cannot finish by itself
  const port = Number(new URL(first.url).port);
  const { sessionId } = await (await post(`${first.url}/sessions`)).json() as { sessionId: string };
  const versions = async (url: string) =>
    ((await (await fetch(`${url}/demo/designs/${sessionId}`)).json()) as { versions: Record<string, unknown>[] }).versions;

  const prompt = await post(`${first.url}/sessions/${sessionId}/prompts`, { message: "Build the page.", messageId: "m1", detach: true });
  assert.equal(prompt.status, 200);
  await prompt.body!.cancel();   // detached: walking away from the response must not stop the run

  const before: Event[] = [];
  const following = collect(`${first.url}/sessions/${sessionId}/events?after=0&follow=1`, before);

  // The slow step is the second publish. Wait until its file is on disk and the
  // follower has seen the call start, so the kill lands between effect and result.
  await until("version 2 on disk", async () => (await versions(first.url)).length === 2);
  const inFlight = await until("the second publish to start", () =>
    before.filter((event) => event.type === "tool_execution_start").at(1));
  assert.equal(before.filter((event) => event.type === "tool_execution_end").length, 1, "the second publish must still be running");

  // The kill switch is off unless the server was started with --allow-kill.
  assert.equal((await post(`${first.url}/demo/kill`)).status, 403);

  first.child.kill("SIGKILL");
  await once(first.child, "exit");
  await following;
  assert.notEqual(before.at(-1)?.type, "agent_end", "the run must not have finished before the kill");
  const lastCursor = Math.max(...before.map((event) => event.cursor ?? 0));

  // ---- Second server, same data directory: reopen, resume, follow from the last cursor. ----
  const second = await start(data, port, 50);
  assert.equal((await fetch(`${second.url}/sessions/${sessionId}`)).status, 404, "a new server has no session open");
  assert.equal((await post(`${second.url}/sessions`, { sessionId })).status, 201);
  const reopened = await (await fetch(`${second.url}/sessions/${sessionId}`)).json() as { resumable: boolean };
  assert.equal(reopened.resumable, true);

  const resume = await post(`${second.url}/sessions/${sessionId}/resume`, { detach: true });
  assert.equal(resume.status, 200);
  await resume.body!.cancel();

  const after: Event[] = [];
  await collect(`${second.url}/sessions/${sessionId}/events?after=${lastCursor}&follow=1`, after);
  assert.equal(after.at(-1)?.type, "agent_end");
  assert.equal(after.at(-1)?.status, "completed");

  // ---- The claims. ----
  // 1. Across both server lifetimes the cursors only ever go up: nothing was delivered twice.
  const cursors = [...before, ...after].flatMap((event) => (event.cursor === undefined ? [] : [event.cursor]));
  for (let index = 1; index < cursors.length; index += 1)
    assert.ok(cursors[index] > cursors[index - 1], `cursor ${cursors[index]} follows ${cursors[index - 1]}`);

  // 2. The interrupted publish was run again, as the same execution.
  const rerun = after.find((event) => event.type === "tool_execution_start" && event.rerun === true);
  assert.ok(rerun, "the resumed run must rerun the publish that was in flight");
  assert.equal(rerun.executionId, inFlight.executionId);

  // 3. Three versions, each once. The second was written twice and is still one version.
  const published = await versions(second.url);
  assert.deepEqual(published.map((version) => version.version), [1, 2, 3]);
  assert.deepEqual(published.map((version) => version.attempts), [1, 2, 1]);
  assert.equal(new Set(published.map((version) => version.executionId)).size, 3);
  assert.equal(published[1].executionId, inFlight.executionId);
  assert.deepEqual(readdirSync(join(data, "designs", sessionId)).filter((name) => !name.endsWith(".tmp")).length, 3);

  // 4. The journal agrees: four starts (one of them the rerun) and three results, each for a different call.
  const journal = (await (await fetch(`${second.url}/sessions/${sessionId}/events?after=0&limit=1000`)).json()) as { events: Event[] };
  assert.equal(journal.events.filter((event) => event.type === "tool_execution_start").length, 4);
  const results = journal.events.filter((event) => event.type === "tool_execution_end");
  assert.equal(results.length, 3);
  assert.equal(new Set(results.map((event) => event.toolCallId)).size, 3);

  // 5. The final version is served as a sandboxed document, and the agent API still refuses other origins.
  const design = await fetch(`${second.url}/demo/designs/${sessionId}/3`);
  assert.match(design.headers.get("content-security-policy") ?? "", /^sandbox allow-scripts; default-src 'none'/);
  assert.match(await design.text(), /^<!doctype html>/);
  assert.equal((await post(`${second.url}/sessions`, {}, { Origin: "https://example.com" })).status, 403);
  assert.equal((await post(`${second.url}/sessions`, {}, { Origin: "null" })).status, 403);
  assert.equal((await fetch(`${second.url}/sessions`, { headers: { Origin: `http://127.0.0.1:${port}` } })).status, 200);
});
