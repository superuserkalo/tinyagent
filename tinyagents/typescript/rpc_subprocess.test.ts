import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import test from "node:test";
import type { Item } from "./tiny_agent.ts";

// Durable sessions lock through node:sqlite. Node 22 announces that once on
// stderr; it is the runtime's notice, not a TinyAgent diagnostic.
const SQLITE_WARNING = /^\(node:\d+\) ExperimentalWarning: SQLite is an experimental feature.*\n\(Use `node --trace-warnings.*\n/;

async function until(check: () => boolean): Promise<void> {
  const deadline = Date.now() + 5000;
  while (!check()) {
    if (Date.now() >= deadline) throw new Error("RPC subprocess did not reach the expected state");
    await delay(10);
  }
}

function running(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error instanceof Error && "code" in error && error.code === "ESRCH") return false;
    throw error;
  }
}

function start(args: string[], environment: NodeJS.ProcessEnv = {}) {
  const env = { ...process.env, ...environment };
  delete env.ANTHROPIC_API_KEY;
  delete env.OPENAI_API_KEY;
  delete env.TINYAGENT_PROVIDER;
  // The real CLI validates its key at startup, although state commands use no provider.
  if (args.includes("--mode")) env.OPENAI_API_KEY = "fixture";
  const child = spawn(process.execPath, args, { env, stdio: ["pipe", "pipe", "pipe"] });
  const messages: Item[] = [];
  const malformed: string[] = [];
  const lines = createInterface({ input: child.stdout });
  lines.on("line", (line) => {
    try {
      const value: unknown = JSON.parse(line);
      if (typeof value !== "object" || value === null || Array.isArray(value)) malformed.push(line);
      else messages.push(value as Item);
    } catch {
      malformed.push(line);
    }
  });
  let stderr = "";
  child.stderr.on("data", (chunk: Buffer) => {
    stderr += chunk.toString();
  });
  const closed = new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((resolve, reject) => {
    child.once("error", reject);
    child.once("close", (code, signal) => resolve({ code, signal }));
  });
  return { child, messages, malformed, closed, stderr: () => stderr };
}

for (const phase of ["provider", "bash"] as const) {
  for (const stop of ["eof", "interrupt"] as const) {
    test("RPC subprocess " + stop + " cancels " + phase + " and exits cleanly", async () => {
      const temporary = mkdtempSync(join(tmpdir(), "tinyagent-rpc-"));
      const pidFile = join(temporary, "pids");
      const fixture = start([fileURLToPath(new URL("./rpc_fixture.ts", import.meta.url))], {
        TINYAGENT_FIXTURE_PID_FILE: pidFile,
      });
      let pids: number[] = [];
      let complete = false;
      try {
        fixture.child.stdin.write(JSON.stringify({ id: "prompt", type: "prompt", message: phase }) + "\n");
        if (phase === "bash") {
          await until(() => existsSync(pidFile) && readFileSync(pidFile, "utf8").trim().split(" ").length === 2);
          pids = readFileSync(pidFile, "utf8").trim().split(" ").map(Number);
          assert.ok(pids.every((pid) => Number.isSafeInteger(pid) && pid > 1 && running(pid)));
        } else {
          await until(() => fixture.messages.some((event) => event.type === "text_delta" && event.delta === "ready"));
        }
        if (stop === "interrupt") {
          fixture.child.stdin.write('{"id":"partial');
          assert.equal(fixture.child.kill("SIGINT"), true);
        } else fixture.child.stdin.end();
        await until(() => fixture.child.exitCode !== null || fixture.child.signalCode !== null);
        assert.deepEqual(await fixture.closed, { code: 0, signal: null });
        assert.deepEqual(fixture.malformed, []);
        assert.equal(fixture.messages[0].command, "prompt");
        const events = fixture.messages.filter((message) => typeof message.runId === "string");
        assert.equal(events[0].type, "agent_start");
        assert.deepEqual(
          events.map((event) => event.sequence),
          events.map((_, index) => index + 1),
        );
        assert.equal(new Set(events.map((event) => event.runId)).size, 1);
        assert.equal(events.filter((event) => event.type === "agent_end").length, 1);
        assert.equal(events.at(-1)?.status, "aborted");
        if (phase === "bash") {
          assert.deepEqual(
            events
              .filter((event) => typeof event.type === "string" && event.type.startsWith("tool_"))
              .map((event) => event.type),
            ["tool_execution_start", "tool_execution_end"],
          );
          assert.equal(events.find((event) => event.type === "tool_execution_end")?.isError, true);
          await until(() => pids.every((pid) => !running(pid)));
          assert.equal(fixture.stderr(), "");
        } else assert.equal(fixture.stderr(), "provider released\n");
        complete = true;
      } finally {
        if (!complete && pids[0] && running(pids[0])) process.kill(-pids[0], "SIGKILL");
        if (fixture.child.exitCode === null && fixture.child.signalCode === null) fixture.child.kill("SIGKILL");
        await fixture.closed;
        rmSync(temporary, { recursive: true });
      }
    });
  }
}

test("the coding CLI exits on interrupt while RPC stdin remains open", async () => {
  // The CLI stores sessions under the working directory unless told otherwise.
  const sessions = mkdtempSync(join(tmpdir(), "tinyagent-rpc-"));
  const fixture = start([fileURLToPath(new URL("./tiny_agent.ts", import.meta.url)), "--mode", "rpc"], {
    TINYAGENT_SESSION_DIR: sessions,
  });
  try {
    fixture.child.stdin.write('{"id":"state","type":"get_state"}\n');
    await until(() => fixture.messages.some((message) => message.id === "state"));
    assert.equal(fixture.child.kill("SIGINT"), true);
    await until(() => fixture.child.exitCode !== null || fixture.child.signalCode !== null);
    assert.deepEqual(await fixture.closed, { code: 0, signal: null });
    assert.deepEqual(fixture.malformed, []);
    assert.equal(fixture.stderr().replace(SQLITE_WARNING, ""), "");
  } finally {
    if (fixture.child.exitCode === null && fixture.child.signalCode === null) fixture.child.kill("SIGKILL");
    await fixture.closed;
    rmSync(sessions, { recursive: true });
  }
});

for (const stop of ["eof", "interrupt"] as const) {
  test("RPC subprocess " + stop + " releases a blocked stdout pipe", async () => {
    const fixture = start([fileURLToPath(new URL("./rpc_fixture.ts", import.meta.url))]);
    fixture.child.stdout.pause();
    try {
      fixture.child.stdin.write('{"type":"prompt","message":"flood"}\n');
      await until(() => fixture.stderr().includes("flood ready\n"));
      await delay(50);
      if (stop === "interrupt") assert.equal(fixture.child.kill("SIGINT"), true);
      else fixture.child.stdin.end();
      await until(() => fixture.child.exitCode !== null || fixture.child.signalCode !== null).catch(
        (error: unknown) => {
          throw new Error(String(error) + "; stderr: " + fixture.stderr());
        },
      );
      fixture.child.stdout.resume();
      assert.deepEqual(await fixture.closed, { code: 0, signal: null });
      assert.equal(fixture.stderr(), "flood ready\nprovider released\n");
    } finally {
      fixture.child.stdout.resume();
      if (fixture.child.exitCode === null && fixture.child.signalCode === null) fixture.child.kill("SIGKILL");
      await fixture.closed;
    }
  });
}
