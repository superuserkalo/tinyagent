import assert from "node:assert/strict";
import { PassThrough, Readable, Writable } from "node:stream";
import { setImmediate as nextTick, setTimeout as delay } from "node:timers/promises";
import test from "node:test";
import { Agent, MAX_JSONL_LINE_BYTES, RpcSession, runRpc, type Item, type Provider } from "./tiny_agent.ts";

function jsonObject(value: string): Item {
  const parsed: unknown = JSON.parse(value);
  assert.ok(typeof parsed === "object" && parsed !== null && !Array.isArray(parsed));
  return parsed as Item;
}

async function within<T>(promise: Promise<T>, timeoutMs = 1000): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error("RPC did not stop")), timeoutMs);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

test("RPC output failure releases the provider and stops reading open input", async () => {
  const failure = new Error("broken RPC output");
  let released = false;
  const provider: Provider = {
    async *stream(request) {
      try {
        yield { type: "text_delta", delta: "fail this write" };
        throw new Error("provider should be closed at the failed write");
      } finally {
        assert.equal(request.signal.aborted, true);
        released = true;
      }
    },
  };
  const input = new PassThrough();
  const output = new Writable({
    write(chunk: Buffer, _encoding, callback) {
      callback(jsonObject(chunk.toString()).type === "text_delta" ? failure : undefined);
    },
  });
  // The embedding application may have its own listener; the driver must preserve it.
  const observed: Error[] = [];
  output.on("error", (error: Error) => observed.push(error));
  const listenerCount = output.listenerCount("error");
  const running = runRpc(new Agent({ model: "fixture", provider }), input, output);
  const outcome = running.then(
    () => undefined,
    (error: unknown) => error,
  );
  input.write('{"id":"prompt","type":"prompt","message":"go"}\n');
  try {
    assert.equal(await within(outcome), failure);
    assert.equal(released, true);
    assert.equal(input.destroyed, true);
    assert.deepEqual(observed, [failure]);
    assert.equal(output.listenerCount("error"), listenerCount);
  } finally {
    input.destroy();
    await outcome;
  }
});

test("RPC oversized command replies cancel the run and keep the connection usable", async () => {
  let started!: () => void;
  const ready = new Promise<void>((resolve) => {
    started = resolve;
  });
  let released = false;
  const provider: Provider = {
    async *stream(request) {
      try {
        started();
        if (!request.signal.aborted)
          await new Promise<void>((resolve) =>
            request.signal.addEventListener("abort", () => resolve(), { once: true }),
          );
        return { text: "too late", assistant: {}, output: [], toolCalls: [] };
      } finally {
        released = true;
      }
    },
  };
  const oversized = JSON.stringify({ id: "x".repeat(MAX_JSONL_LINE_BYTES - 50), type: "get_state" });
  assert.ok(Buffer.byteLength(oversized) <= MAX_JSONL_LINE_BYTES);
  const input = Readable.from(
    (async function* () {
      yield '{"id":"prompt","type":"prompt","message":"wait"}\n';
      await ready;
      yield oversized + '\n{"id":"after","type":"get_state"}\n';
    })(),
  );
  const messages: Item[] = [];
  const output = new Writable({
    write(chunk: Buffer, _encoding, callback) {
      messages.push(jsonObject(chunk.toString()));
      callback();
    },
  });
  await within(runRpc(new Agent({ model: "fixture", provider }), input, output));
  assert.equal(messages.filter((message) => message.type === "protocol_error").length, 1);
  assert.equal((messages.find((message) => message.type === "protocol_error")?.error as Item).code, "output_too_large");
  assert.deepEqual(messages.find((message) => message.id === "after")?.data, {
    protocolVersion: 1,
    status: "idle",
    runId: null,
    messageCount: 0,
  });
  assert.equal(messages.find((message) => message.type === "agent_end")?.status, "aborted");
  assert.equal(released, true);
  assert.equal(output.destroyed, false);
});

test("RPC output closure releases a pending write and provider", async () => {
  let blocked!: () => void;
  const writing = new Promise<void>((resolve) => {
    blocked = resolve;
  });
  let released = false;
  const provider: Provider = {
    async *stream(request) {
      try {
        yield { type: "text_delta", delta: "blocked" };
        throw new Error("provider should be closed while writing");
      } finally {
        assert.equal(request.signal.aborted, true);
        released = true;
      }
    },
  };
  const input = new PassThrough();
  const output = new Writable({
    write(chunk: Buffer, _encoding, callback) {
      if (jsonObject(chunk.toString()).type === "text_delta") blocked();
      else callback();
    },
  });
  const running = runRpc(new Agent({ model: "fixture", provider }), input, output);
  const outcome = running.then(
    () => undefined,
    (error: unknown) => error,
  );
  input.write('{"type":"prompt","message":"go"}\n');
  try {
    await within(writing);
    output.destroy();
    const error = await within(outcome);
    assert.ok(error instanceof Error);
    assert.match(error.message, /RPC output closed/);
    assert.equal(released, true);
    assert.equal(input.destroyed, true);
    assert.equal(output.listenerCount("error"), 0);
    assert.equal(output.listenerCount("close"), 0);
  } finally {
    output.destroy();
    input.destroy();
    await outcome;
  }
});

test("RPC rejects an already closed output without waiting for input", async () => {
  const input = new PassThrough();
  const output = new Writable({
    write(_chunk, _encoding, callback) {
      callback();
    },
  });
  output.destroy();
  try {
    await assert.rejects(within(runRpc(new Agent({ model: "fixture" }), input, output)), /RPC output closed/);
    assert.equal(output.listenerCount("error"), 0);
  } finally {
    input.destroy();
  }
});

for (const stop of ["abort", "new_session", "eof", "invalid_utf8", "oversized_reply"] as const) {
  test("RPC " + stop + " releases a provider behind stalled output", async () => {
    let blocked!: () => void;
    const writing = new Promise<void>((resolve) => {
      blocked = resolve;
    });
    let released = false;
    const provider: Provider = {
      async *stream(request) {
        try {
          yield { type: "text_delta", delta: "blocked" };
          throw new Error("a stalled consumer must prevent further provider reads");
        } finally {
          assert.equal(request.signal.aborted, true);
          released = true;
        }
      },
    };
    const input = new PassThrough();
    const output = new Writable({
      write(chunk: Buffer, _encoding, callback) {
        if (jsonObject(chunk.toString()).type === "text_delta") blocked();
        else callback();
      },
    });
    const outcome = runRpc(new Agent({ model: "fixture", provider }), input, output).then(
      () => undefined,
      (error: unknown) => error,
    );
    input.write('{"type":"prompt","message":"go"}\n');
    try {
      await within(writing);
      if (stop === "eof") input.end();
      else if (stop === "invalid_utf8") input.write(Buffer.from([0xff, 0x0a]));
      else if (stop === "oversized_reply")
        input.write(JSON.stringify({ id: "x".repeat(MAX_JSONL_LINE_BYTES - 50), type: "get_state" }) + "\n");
      else input.write(JSON.stringify({ type: stop }) + "\n");
      assert.equal(await within(outcome, 3000), undefined);
      assert.equal(released, true);
      assert.equal(input.destroyed, true);
      assert.equal(output.destroyed, true);
      assert.equal(output.listenerCount("error"), 0);
    } finally {
      output.destroy();
      input.destroy();
      await outcome;
    }
  });
}

test("RPC EOF releases a stalled command reply with no active run", async () => {
  const input = new PassThrough();
  const output = new Writable({ write() {} });
  const outcome = runRpc(new Agent({ model: "fixture" }), input, output).then(
    () => undefined,
    (error: unknown) => error,
  );
  input.end('{"id":"state","type":"get_state"}\n');
  try {
    assert.equal(await within(outcome, 3000), undefined);
    assert.equal(output.destroyed, true);
  } finally {
    output.destroy();
    input.destroy();
    await outcome;
  }
});

test("RPC cancellation preserves a connection that drains within the grace period", async () => {
  let drain: (() => void) | undefined;
  const messages: Item[] = [];
  const provider: Provider = {
    async *stream() {
      yield { type: "text_delta", delta: "held" };
      return { text: "too late", assistant: {}, output: [], toolCalls: [] };
    },
  };
  const input = new PassThrough();
  const output = new Writable({
    write(chunk: Buffer, _encoding, callback) {
      const message = jsonObject(chunk.toString());
      messages.push(message);
      if (message.type === "text_delta") drain = callback;
      else callback();
    },
  });
  const outcome = runRpc(new Agent({ model: "fixture", provider }), input, output);
  input.write('{"type":"prompt","message":"go"}\n');
  try {
    while (!drain) await nextTick();
    input.write('{"id":"abort","type":"abort"}\n');
    await nextTick();
    drain();
    await delay(1100);
    assert.equal(output.destroyed, false);
    assert.equal(messages.find((message) => message.type === "agent_end")?.status, "aborted");
    input.end('{"id":"after","type":"get_state"}\n');
    await within(outcome);
    assert.deepEqual(messages.find((message) => message.id === "after")?.data, {
      protocolVersion: 1,
      status: "idle",
      runId: null,
      messageCount: 0,
    });
    assert.equal(output.destroyed, false);
  } finally {
    output.destroy();
    input.destroy();
    await outcome.catch(() => {});
  }
});

test("RPC session close waits for prompt acknowledgement cleanup", async () => {
  for (const rejectAcknowledgement of [false, true]) {
    let release!: () => void;
    const cleanup = new Promise<void>((resolve) => {
      release = resolve;
    });
    let started!: () => void;
    const ready = new Promise<void>((resolve) => {
      started = resolve;
    });
    let signal: AbortSignal | undefined;
    let called = false;
    const failure = new Error("acknowledgement failed");
    const agent = new Agent({
      model: "fixture",
      transport() {
        called = true;
        return [];
      },
    });
    const session = new RpcSession(agent, async (value, runSignal) => {
      if ("command" in value && value.command === "prompt") {
        signal = runSignal;
        started();
        await cleanup;
        if (rejectAcknowledgement) throw failure;
      }
    });
    const prompt = session.handle({ type: "prompt", message: "go" }).then(
      () => undefined,
      (error: unknown) => error,
    );
    await within(ready);
    let closed = false;
    const closing = session.close().then(() => {
      closed = true;
    });
    await nextTick();
    assert.equal(closed, false);
    assert.equal(signal?.aborted, true);
    release();
    assert.equal(await within(prompt), rejectAcknowledgement ? failure : undefined);
    await within(closing);
    assert.equal(called, false);
    assert.equal(await session.waitIdle(), true);
  }
});

test("a closed RPC session refuses to start work or open another session", async () => {
  let calls = 0;
  const provider: Provider = {
    async *stream() {
      calls += 1;
      throw new Error("a closed session must not call the provider");
    },
  };
  const emitted: Item[] = [];
  const session = new RpcSession(new Agent({ model: "test", provider }), (value) => {
    emitted.push(value as Item);
  });
  await session.close();
  await session.close();
  const closed = session.session;
  for (const type of ["prompt", "compact", "new_session", "resume_session"]) {
    await session.handle({ id: type, type, message: "go", sessionId: "s_other" });
    const reply = emitted.at(-1) as { id?: string; error?: { code?: string } };
    assert.equal(reply.id, type);
    assert.equal(reply.error?.code, "session_closed");
  }
  assert.equal(session.session, closed);
  assert.equal(calls, 0);
});
