import assert from "node:assert/strict";
import { setTimeout as delay } from "node:timers/promises";
import test from "node:test";
import { Agent, MAX_JSONL_LINE_BYTES, ProviderError, Tool, type Item, type ProviderTurn } from "./tiny_agent.ts";
import { HttpFixture, body, deferred, json, object, until } from "./http_fixture.ts";

function turn(text: string): ProviderTurn {
  return { text, assistant: { role: "assistant", content: [{ type: "text", text }] }, output: [], toolCalls: [] };
}

test(
  "native HTTP framing serves health and independent session histories without TCP",
  { timeout: 5000 },
  async (t) => {
    const histories: Item[][] = [];
    const fixture = new HttpFixture(
      new Agent({
        model: "fixture",
        provider: {
          async *stream(request) {
            histories.push(structuredClone(request.history));
            yield { type: "text_delta", delta: "café\n" };
            return turn("café\n");
          },
        },
      }),
    );
    t.after(() => fixture.dispose());
    const health = await fixture.send("GET", "/health");
    assert.equal(health.statusCode, 200);
    assert.deepEqual(await json(health), { status: "ok", protocolVersion: 2 });
    const first = await fixture.session();
    const second = await fixture.session();
    assert.notEqual(first, second);
    for (const [id, prompt] of [
      [first, "one"],
      [first, "two"],
      [second, "other"],
    ]) {
      const response = await fixture.prompt(id, prompt);
      assert.equal(response.statusCode, 200);
      assert.equal(response.headers["content-type"], "application/jsonl; charset=utf-8");
      const events = (await body(response))
        .trimEnd()
        .split("\n")
        .map((line) => object(JSON.parse(line)));
      assert.deepEqual(
        events.map((event) => event.type),
        ["agent_start", "text_delta", "agent_end"],
      );
      assert.deepEqual(
        events.map((event) => event.sequence),
        [1, 2, 3],
      );
      assert.equal(new Set(events.map((event) => event.runId)).size, 1);
      assert.equal(events.at(-1)?.status, "completed");
    }
    assert.deepEqual(
      histories.map((history) => history.length),
      [1, 3, 1],
    );
  },
);

test("G05 F21 HTTP commits and releases the run before the final framing reaches the client", { timeout: 5000 }, async (t) => {
  const histories: Item[][] = [];
  const fixture = new HttpFixture(
    new Agent({
      model: "fixture",
      provider: {
        async *stream(request) {
          histories.push(structuredClone(request.history));
          return turn("done");
        },
      },
    }),
  );
  t.after(() => fixture.dispose());
  const id = await fixture.session();
  await body(await fixture.prompt(id, "keep"));
  const ending = deferred();
  const release = deferred();
  const exchange = fixture.open(
    "POST",
    "/sessions/" + id + "/prompts",
    { "Content-Type": "application/json" },
    async (chunk) => {
      if (chunk.includes(Buffer.from("0\r\n\r\n"))) {
        ending.resolve();
        await release.promise;
        throw new Error("final HTTP framing failed");
      }
    },
  );
  exchange.request.end('{"message":"discard"}');
  const response = await exchange.response;
  const reading = body(response).then(
    () => undefined,
    (error: unknown) => error,
  );
  try {
    await ending.promise;
    // The run is recorded and the session idle before its last line is delivered: the next prompt is accepted.
    const next = await fixture.prompt(id, "at once");
    assert.equal(next.statusCode, 200);
    await body(next);
  } finally {
    release.resolve();
  }
  assert.ok((await reading) instanceof Error);
  const recovered = await fixture.prompt(id, "retry");
  await body(recovered);
  assert.deepEqual(
    histories.map((history) => history.length),
    [1, 3, 5, 7],
  );
});

for (const stop of ["cancel", "delete", "shutdown"] as const) {
  test("HTTP " + stop + " releases a provider behind stalled response output", { timeout: 5000 }, async (t) => {
    let produced = 0;
    let released = false;
    const fixture = new HttpFixture(
      new Agent({
        model: "fixture",
        provider: {
          async *stream(request) {
            try {
              while (!request.signal.aborted) {
                produced++;
                yield { type: "text_delta", delta: "x".repeat(131_072) };
              }
              return turn("cancelled");
            } finally {
              released = true;
            }
          },
        },
      }),
    );
    t.after(() => fixture.dispose());
    const id = await fixture.session();
    const response = await fixture.prompt(id, "wait");
    await until(() => produced > 0);
    await delay(20);
    const count = produced;
    assert.ok(count <= 4, "bounded stream buffers must not accumulate provider events");
    await delay(20);
    assert.equal(produced, count, "a stalled client must stop provider production");
    if (stop === "shutdown") {
      const closing = fixture.application.close();
      await until(() => released);
      await closing;
    } else {
      const cancelled = await fixture.send(
        stop === "delete" ? "DELETE" : "POST",
        "/sessions/" + id + (stop === "cancel" ? "/cancel" : ""),
      );
      assert.equal(cancelled.statusCode, stop === "delete" ? 204 : 200);
      await body(cancelled);
      await until(() => released);
    }
    assert.equal(response.destroyed, true);
  });
}

test("HTTP failures and oversized output preserve completed history", { timeout: 5000 }, async (t) => {
  const histories: Item[][] = [];
  let closed = false;
  const fixture = new HttpFixture(
    new Agent({
      model: "fixture",
      provider: {
        async *stream(request) {
          histories.push(structuredClone(request.history));
          const prompt = request.history.at(-1)?.content;
          if (prompt === "fail") {
            request.history[0].content = "changed";
            yield { type: "text_delta", delta: "partial" };
            throw new ProviderError("provider failed");
          }
          if (prompt === "large") {
            try {
              yield { type: "text_delta", delta: "x".repeat(MAX_JSONL_LINE_BYTES) };
            } finally {
              closed = true;
            }
          }
          return turn("done");
        },
      },
    }),
  );
  t.after(() => fixture.dispose());
  const id = await fixture.session();
  await body(await fixture.prompt(id, "keep"));
  for (const [prompt, code] of [
    ["fail", "provider_error"],
    ["large", "output_too_large"],
  ]) {
    const text = await body(await fixture.prompt(id, prompt));
    const events = text
      .trimEnd()
      .split("\n")
      .map((line) => object(JSON.parse(line)));
    assert.equal(events.filter((event) => event.type === "agent_end").length, 1);
    assert.equal(events.at(-1)?.status, "failed");
    assert.equal(object(events.at(-1)?.error).code, code);
    assert.ok(text.split("\n").every((line) => Buffer.byteLength(line) <= MAX_JSONL_LINE_BYTES));
  }
  await body(await fixture.prompt(id, "retry"));
  assert.equal(closed, true);
  assert.deepEqual(
    histories.map((history) => history.length),
    [1, 3, 3, 3],
  );
  assert.ok(histories.every((history) => history[0].content === "keep"));
});

test("HTTP rejects busy prompts while other sessions run and confirms cancellation", { timeout: 5000 }, async (t) => {
  const started = deferred();
  const lengths: number[] = [];
  const fixture = new HttpFixture(
    new Agent({
      model: "fixture",
      provider: {
        async *stream(request) {
          lengths.push(request.history.length);
          if (request.history.at(-1)?.content === "wait") {
            yield { type: "text_delta", delta: "waiting" };
            started.resolve();
            if (!request.signal.aborted)
              await new Promise<void>((resolve) =>
                request.signal.addEventListener("abort", () => resolve(), { once: true }),
              );
          }
          return turn("done");
        },
      },
    }),
  );
  t.after(() => fixture.dispose());
  const id = await fixture.session();
  const other = await fixture.session();
  const pending = await fixture.prompt(id, "wait");
  await started.promise;
  const busy = await fixture.prompt(id, "busy");
  assert.equal(busy.statusCode, 409);
  assert.equal(object((await json(busy)).error).code, "session_busy");
  await body(await fixture.prompt(other, "other"));
  const cancelled = await json(await fixture.send("POST", "/sessions/" + id + "/cancel"));
  assert.equal(cancelled.accepted, true);
  const events = (await body(pending))
    .trimEnd()
    .split("\n")
    .map((line) => object(JSON.parse(line)));
  assert.equal(events.at(-1)?.status, "aborted");
  assert.equal(events[0].runId, cancelled.runId);
  await body(await fixture.prompt(id, "after"));
  assert.deepEqual(lengths, [1, 1, 1]);
  assert.deepEqual(await json(await fixture.send("POST", "/sessions/" + id + "/cancel")), {
    accepted: false,
    runId: null,
  });
});

for (const stop of ["delete", "disconnect"] as const) {
  test("HTTP " + stop + " awaits cooperative tool cleanup and retains prior history", { timeout: 5000 }, async (t) => {
    const started = deferred();
    const aborted = deferred();
    const release = deferred();
    let cleaned = false;
    const lengths: number[] = [];
    const wait = new Tool({
      name: "wait",
      description: "Wait for cancellation",
      parameters: { type: "object" },
      async handler(_arguments, signal) {
        started.resolve();
        if (!signal.aborted)
          await new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
        aborted.resolve();
        await release.promise;
        cleaned = true;
        return "cancelled";
      },
    });
    const fixture = new HttpFixture(
      new Agent({
        model: "fixture",
        tools: [wait],
        provider: {
          async *stream(request) {
            lengths.push(request.history.length);
            if (request.history.at(-1)?.content === "wait")
              return { ...turn(""), toolCalls: [{ callId: "waiting", name: "wait", arguments: {} }] };
            return turn("done");
          },
        },
      }),
    );
    t.after(async () => {
      release.resolve();
      await fixture.dispose();
    });
    const id = await fixture.session();
    await body(await fixture.prompt(id, "keep"));
    const pending = await fixture.prompt(id, "wait");
    await started.promise;
    if (stop === "delete") {
      let deleted = false;
      const deleting = fixture.send("DELETE", "/sessions/" + id).then((response) => {
        deleted = true;
        return response;
      });
      await aborted.promise;
      // The handler is given time to clean up after cancellation (G24 bounds that time at one second).
      await delay(300);
      assert.equal(deleted, false);
      assert.equal(pending.destroyed, false, "cleanup must not close a response whose writes are draining");
      const missing = await fixture.prompt(id, "missing");
      assert.equal(missing.statusCode, 404);
      await body(missing);
      release.resolve();
      const response = await deleting;
      assert.equal(response.statusCode, 204);
      await body(response);
      const events = (await body(pending))
        .trimEnd()
        .split("\n")
        .map((line) => object(JSON.parse(line)));
      assert.deepEqual(
        events.map((event) => event.type),
        ["agent_start", "tool_execution_start", "tool_execution_end", "agent_end"],
      );
      assert.equal(events.at(-1)?.status, "aborted");
    } else {
      pending.destroy();
      await aborted.promise;
      release.resolve();
      await until(() => cleaned);
      // The cancelled run left a tool unfinished. That is recorded, and the next prompt is accepted.
      const page = await json(await fixture.send("GET", `/sessions/${id}?view=records&limit=100`));
      assert.deepEqual((page.records as Item[]).slice(-3).map((record) => [record.type, record.interrupted ?? record.status]),
        [["tool_intent", undefined], ["tool_result", true], ["run_failed", "aborted"]]);
      assert.equal(page.recoveryRequired, false);
      await body(await fixture.prompt(id, "retry"));
      assert.deepEqual(lengths, [1, 3, 3]);
    }
    assert.equal(cleaned, true);
  });
}

test("HTTP rechecks a session deleted while a cancel body is arriving", { timeout: 5000 }, async (t) => {
  const fixture = new HttpFixture(new Agent({ model: "fixture", transport: () => [] }));
  t.after(() => fixture.dispose());
  const id = await fixture.session();
  const arrived = deferred();
  fixture.application.server.once("request", () => arrived.resolve());
  const exchange = fixture.open("POST", "/sessions/" + id + "/cancel", { "Content-Type": "application/json" });
  exchange.request.write("{");
  await arrived.promise;
  const removed = await fixture.send("DELETE", "/sessions/" + id);
  assert.equal(removed.statusCode, 204);
  await body(removed);
  exchange.request.end("}");
  const stale = await exchange.response;
  assert.equal(stale.statusCode, 404);
  assert.equal(object((await json(stale)).error).code, "session_not_found");
});
