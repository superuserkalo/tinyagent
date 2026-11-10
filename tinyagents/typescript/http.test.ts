import assert from "node:assert/strict";
import { request as httpRequest } from "node:http";
import test, { type TestContext } from "node:test";
import {
  Agent,
  MAX_HTTP_BODY_BYTES,
  MAX_JSONL_LINE_BYTES,
  ProviderError,
  Tool,
  serve,
  type AgentServer,
  type Item,
  type Provider,
  type ProviderTurn,
} from "./tiny_agent.ts";

function object(value: unknown): Item {
  assert.ok(typeof value === "object" && value !== null && !Array.isArray(value));
  return value as Item;
}

function turn(text: string): ProviderTurn {
  return { text, assistant: { role: "assistant", content: [{ type: "text", text }] }, output: [], toolCalls: [] };
}

function deferred() {
  let resolve: () => void = () => {};
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

async function start(t: TestContext, provider: Provider): Promise<AgentServer> {
  const server = await serve(new Agent({ model: "test", provider }), { port: 0 });
  t.after(() => server.close());
  return server;
}

async function session(server: AgentServer, token?: string): Promise<string> {
  const response = await fetch(`${server.url}/sessions`, {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  assert.equal(response.status, 201);
  const value = object(await response.json());
  assert.equal(typeof value.sessionId, "string");
  assert.match(String(value.sessionId), /^s_[a-f0-9]{32}$/);
  return String(value.sessionId);
}

function prompt(server: AgentServer, id: string, message: string): Promise<Response> {
  return fetch(`${server.url}/sessions/${id}/prompts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
}

async function events(response: Response): Promise<Item[]> {
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-type"), "application/jsonl; charset=utf-8");
  const text = await response.text();
  assert.ok(text.endsWith("\n"));
  const values = text
    .trimEnd()
    .split("\n")
    .map((line) => object(JSON.parse(line)));
  assert.equal(values[0].type, "agent_start");
  assert.equal(values.at(-1)?.type, "agent_end");
  assert.equal(values.filter((event) => event.type === "agent_end").length, 1);
  assert.deepEqual(
    values.map((event) => event.sequence),
    values.map((_, index) => index + 1),
  );
  assert.equal(new Set(values.map((event) => event.runId)).size, 1);
  return values;
}

test("HTTP defaults, authentication, exposure, and unknown routes", async (t) => {
  const agent = new Agent({ model: "test", transport: () => [] });
  await assert.rejects(serve(agent, { host: "0.0.0.0", port: 0 }), /remote binding/);
  await assert.rejects(serve(agent, { host: "0.0.0.0", port: 0, token: "secret" }), /remote binding/);
  await assert.rejects(serve(agent, { host: "0.0.0.0", port: 0, allowRemote: true }), /remote binding/);
  await assert.rejects(serve(agent, { port: 0, token: "" }), /token/);
  const server = await serve(agent, { port: 0, token: "secret" });
  t.after(() => server.close());
  assert.match(server.url, /^http:\/\/127\.0\.0\.1:/);
  for (const authorization of [undefined, "Bearer wrong", "secret"]) {
    const response = await fetch(`${server.url}/health`, {
      headers: authorization ? { Authorization: authorization } : {},
    });
    assert.equal(response.status, 401);
    assert.equal(object(object(await response.json()).error).code, "unauthorized");
    assert.equal(response.headers.get("access-control-allow-origin"), null);
  }
  const health = await fetch(`${server.url}/health`, { headers: { Authorization: "Bearer secret" } });
  assert.deepEqual(await health.json(), { status: "ok", protocolVersion: 1 });
  const id = await session(server, "secret");
  assert.ok(id);
  const unknown = await fetch(`${server.url}/missing`, { headers: { Authorization: "Bearer secret" } });
  assert.equal(unknown.status, 404);
  await unknown.arrayBuffer();
});

test("HTTP streams deltas and commits independent session histories", async (t) => {
  const histories: Item[][] = [];
  const server = await start(t, {
    async *stream(request) {
      histories.push(structuredClone(request.history));
      yield { type: "text_delta", delta: "Hello\n" };
      yield { type: "text_delta", delta: "world" };
      return turn("Hello\nworld");
    },
  });
  const one = await session(server);
  const two = await session(server);
  assert.notEqual(one, two);
  const emitted = await events(await prompt(server, one, "first"));
  assert.deepEqual(
    emitted.map((event) => event.type),
    ["agent_start", "text_delta", "text_delta", "agent_end"],
  );
  assert.equal(emitted.at(-1)?.text, "Hello\nworld");
  await events(await prompt(server, one, "second"));
  await events(await prompt(server, two, "other"));
  assert.deepEqual(
    histories.map((history) => history.length),
    [1, 3, 1],
  );
  assert.equal(histories[1][0].content, "first");
  const idle = await fetch(`${server.url}/sessions/${one}/cancel`, { method: "POST" });
  assert.deepEqual(await idle.json(), { accepted: false, runId: null });
});

test("HTTP provider failure rolls back even when a provider mutates supplied history", async (t) => {
  let count = 0;
  const server = await start(t, {
    async *stream(request) {
      count += 1;
      if (count === 2) {
        request.history[0].content = "corrupted";
        yield { type: "text_delta", delta: "partial" };
        throw new ProviderError("provider failed");
      }
      if (count === 3) {
        assert.equal(request.history.length, 3);
        assert.equal(request.history[0].content, "keep");
      }
      return turn("done");
    },
  });
  const id = await session(server);
  await events(await prompt(server, id, "keep"));
  const failed = await events(await prompt(server, id, "discard"));
  assert.equal(failed.at(-1)?.status, "failed");
  assert.equal(object(failed.at(-1)?.error).code, "provider_error");
  const recovered = await events(await prompt(server, id, "retry"));
  assert.equal(recovered.at(-1)?.status, "completed");
});

test("HTTP rejects busy prompts, permits concurrent sessions, and confirms cancellation", async (t) => {
  const waiting = deferred();
  const server = await start(t, {
    async *stream(request) {
      if (request.history.at(-1)?.content === "wait") {
        yield { type: "text_delta", delta: "waiting" };
        waiting.resolve();
        if (!request.signal.aborted)
          await new Promise<void>((resolve) =>
            request.signal.addEventListener("abort", () => resolve(), { once: true }),
          );
      }
      return turn("done");
    },
  });
  const id = await session(server);
  const other = await session(server);
  const pending = prompt(server, id, "wait");
  await waiting.promise;
  const busy = await prompt(server, id, "busy");
  assert.equal(busy.status, 409);
  assert.equal(object(object(await busy.json()).error).code, "session_busy");
  assert.equal((await events(await prompt(server, other, "go"))).at(-1)?.status, "completed");
  const cancelled = await fetch(`${server.url}/sessions/${id}/cancel`, { method: "POST" });
  const cancellation = object(await cancelled.json());
  assert.equal(cancellation.accepted, true);
  const aborted = await events(await pending);
  assert.equal(aborted.at(-1)?.status, "aborted");
  assert.equal(aborted[0].runId, cancellation.runId);
  assert.equal((await events(await prompt(server, id, "again"))).at(-1)?.status, "completed");
});

test("HTTP disconnect cancels tools and retains prior history", async (t) => {
  const toolStarted = deferred();
  const toolStopped = deferred();
  let runs = 0;
  const agent = new Agent({
    model: "test",
    tools: [
      new Tool({
        name: "wait",
        description: "Wait",
        parameters: { type: "object" },
        async handler(_arguments, signal) {
          toolStarted.resolve();
          try {
            if (!signal.aborted)
              await new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve(), { once: true }));
            return "cancelled";
          } finally {
            toolStopped.resolve();
          }
        },
      }),
    ],
    provider: {
      async *stream(request) {
        runs += 1;
        if (runs === 2)
          return {
            ...turn(""),
            toolCalls: [{ callId: "tool_1", name: "wait", arguments: {} }],
          };
        if (runs === 3) assert.equal(request.history.length, 4);
        return turn("done");
      },
    },
  });
  const server = await serve(agent, { port: 0 });
  t.after(() => server.close());
  const id = await session(server);
  await events(await prompt(server, id, "keep"));
  const controller = new AbortController();
  const response = await fetch(`${server.url}/sessions/${id}/prompts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: "disconnect" }),
    signal: controller.signal,
  });
  await toolStarted.promise;
  controller.abort();
  await response.body?.cancel().catch(() => {});
  await toolStopped.promise;
  const rejected = await prompt(server, id, "retry");
  assert.equal(rejected.status, 409);
  await rejected.arrayBuffer();
  const reconciled = await fetch(`${server.url}/sessions/${id}/reconcile`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note: "The wait tool stopped without external effects." }),
  });
  assert.equal(reconciled.status, 200);
  await reconciled.arrayBuffer();
  const recovered = await events(await prompt(server, id, "retry"));
  assert.equal(recovered.at(-1)?.status, "completed");
  assert.equal(runs, 3);
  // Deletion waits for run cleanup before acknowledging, including generator cleanup.
  const deleted = await fetch(`${server.url}/sessions/${id}`, { method: "DELETE" });
  assert.equal(deleted.status, 204);
  assert.equal((await prompt(server, id, "missing")).status, 404);
});

test("HTTP deletion and shutdown await active provider cleanup", async (t) => {
  let cleaned = 0;
  const server = await start(t, {
    async *stream(request) {
      try {
        yield { type: "text_delta", delta: "waiting" };
        if (!request.signal.aborted)
          await new Promise<void>((resolve) =>
            request.signal.addEventListener("abort", () => resolve(), { once: true }),
          );
        return turn("cancelled");
      } finally {
        cleaned += 1;
      }
    },
  });
  const one = await session(server);
  const two = await session(server);
  const first = await prompt(server, one, "wait");
  const second = await prompt(server, two, "wait");
  const removed = await fetch(`${server.url}/sessions/${one}`, { method: "DELETE" });
  assert.equal(removed.status, 204);
  assert.equal((await events(first)).at(-1)?.status, "aborted");
  assert.equal(cleaned, 1);
  const secondEvents = events(second);
  await Promise.all([server.close(), server.close()]);
  assert.equal((await secondEvents).at(-1)?.status, "aborted");
  assert.equal(cleaned, 2);
});

test("HTTP validates body types, JSON, UTF-8, and byte limits before streaming", async (t) => {
  const server = await start(t, {
    async *stream() {
      return turn("unused");
    },
  });
  const id = await session(server);
  for (const [body, contentType, expected] of [
    ['{"message":"go"}', "text/plain", 415],
    ["{", "application/json", 400],
    ["[]", "application/json", 400],
    ['{"message":""}', "application/json", 400],
    [Buffer.from([0xff]), "application/json", 400],
    [Buffer.alloc(MAX_HTTP_BODY_BYTES + 1, 32), "application/json", 413],
  ] as const) {
    const response = await fetch(`${server.url}/sessions/${id}/prompts`, {
      method: "POST",
      headers: { "Content-Type": contentType },
      body,
    });
    assert.equal(response.status, expected);
    assert.ok(object(await response.json()).error);
  }
  // No Content-Length: limits also apply to chunked request bodies.
  const status = await new Promise<number | undefined>((resolve, reject) => {
    const request = httpRequest(
      `${server.url}/sessions/${id}/prompts`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      },
      (response) => {
        response.resume();
        response.on("end", () => resolve(response.statusCode));
      },
    );
    request.on("error", reject);
    request.write(Buffer.alloc(MAX_HTTP_BODY_BYTES, 32));
    request.end(" ");
  });
  assert.equal(status, 413);
});

test("HTTP oversized events become one terminal error and close the provider", async (t) => {
  let closed = false;
  const server = await start(t, {
    async *stream() {
      try {
        yield { type: "text_delta", delta: "x".repeat(MAX_JSONL_LINE_BYTES) };
        return turn("too large");
      } finally {
        closed = true;
      }
    },
  });
  const emitted = await events(await prompt(server, await session(server), "go"));
  assert.equal(emitted.at(-1)?.status, "failed");
  assert.equal(object(emitted.at(-1)?.error).code, "output_too_large");
  assert.equal(closed, true);
});
