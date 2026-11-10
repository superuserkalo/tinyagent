import assert from "node:assert/strict";
import test from "node:test";
import { setImmediate as nextTick } from "node:timers/promises";
import {
  Agent,
  AnthropicProvider,
  OpenAIProvider,
  ProviderError,
  Tool,
  type AgentEvent,
  type Item,
  type Provider,
} from "./tiny_agent.ts";
import { MAX_SSE_BYTES, objectArray, objectValue } from "./sse.ts";
import { deferred, fixture, FixtureBody, FixtureFetch } from "./provider_fixture.ts";

test("complete transport requests encrypted reasoning", async () => {
  const requests: Item[] = [];
  await new Agent({
    model: "fixture",
    transport(payload) {
      requests.push(payload);
      return [];
    },
  }).run("go");
  assert.equal(requests.length, 1);
  assert.equal(requests[0].store, false);
  assert.deepEqual(requests[0].include, ["reasoning.encrypted_content"]);
});

function provider(anthropic: boolean, transport: FixtureFetch, timeoutMs = 2000): Provider {
  const options = {
    apiKey: "fixture-key",
    fetch: transport.fetch,
    timeoutMs,
    endpoint: "http://fixture.invalid/events?fixture=1",
  };
  return anthropic ? new AnthropicProvider({ ...options, maxTokens: 123 }) : new OpenAIProvider(options);
}

async function runBytes(bytes: Uint8Array, anthropic = false, chunkSize = 8192) {
  const body = new FixtureBody(bytes, { chunkSize });
  const transport = new FixtureFetch([body]);
  return await new Agent({ model: "fixture", provider: provider(anthropic, transport) }).run("go");
}

function events(...values: Item[]): Buffer {
  return Buffer.from(values.map((value) => "data: " + JSON.stringify(value) + "\n\n").join(""));
}

for (const anthropic of [false, true]) {
  test((anthropic ? "Anthropic" : "OpenAI") + " shared SSE fixtures complete a native tool loop", async () => {
    const transport = new FixtureFetch([
      new FixtureBody(fixture(anthropic, true)),
      new FixtureBody(fixture(anthropic)),
    ]);
    const calls: string[] = [];
    const tool = new Tool({
      name: "echo",
      description: "Return text",
      parameters: {
        type: "object",
        properties: { text: { type: "string" } },
        required: ["text"],
        additionalProperties: false,
      },
      handler(arguments_, signal) {
        assert.equal(signal.aborted, false);
        assert.equal(typeof arguments_.text, "string");
        calls.push(String(arguments_.text));
        return arguments_.text;
      },
    });
    const emitted: AgentEvent[] = [];
    const result = await new Agent({
      model: "fixture",
      instructions: "Fixture instructions",
      provider: provider(anthropic, transport),
      tools: [tool],
    }).run("echo café", [], {
      onEvent(event) {
        emitted.push(event);
      },
    });
    assert.equal(result.text, "done\ncafé");
    assert.deepEqual(calls, ["café"]);
    assert.equal(new Set(emitted.map((event) => event.runId)).size, 1);
    assert.deepEqual(
      emitted.map((event) => event.sequence),
      emitted.map((_, i) => i + 1),
    );
    assert.deepEqual(
      emitted.filter((event) => event.type.startsWith("tool_")).map((event) => event.type),
      ["tool_execution_start", "tool_execution_end"],
    );
    assert.ok(transport.bodies.every((body) => body.cancelled && !body.stream.locked));
    const initial = await transport.payload(0);
    const next = await transport.payload(1);
    assert.equal(initial.model, "fixture");
    assert.equal(initial.stream, true);
    assert.equal(transport.requests[0].url, "http://fixture.invalid/events?fixture=1");
    const definitions = objectArray(initial.tools, "tools");
    if (anthropic) {
      assert.equal(initial.system, "Fixture instructions");
      assert.equal(initial.max_tokens, 123);
      assert.equal(transport.requests[0].headers.get("x-api-key"), "fixture-key");
      assert.equal(transport.requests[0].headers.get("anthropic-version"), "2023-06-01");
      assert.deepEqual(definitions[0].input_schema, tool.parameters);
      const metadata = objectValue(objectValue(result.history[1].providerMetadata, "metadata").message, "message");
      assert.equal(metadata.fixture_metadata, "keep");
      assert.deepEqual(metadata.usage, { input_tokens: 8, output_tokens: 12 });
      const messages = objectArray(next.messages, "messages");
      assert.deepEqual(messages[1].content, metadata.content);
      assert.equal(objectArray(metadata.content, "content")[0].signature, "signed-fixture");
      assert.equal(objectArray(messages[2].content, "content")[0].content, "café");
    } else {
      assert.equal(initial.instructions, "Fixture instructions");
      assert.equal(initial.store, false);
      for (const payload of [initial, next])
        assert.deepEqual(payload.include, ["reasoning.encrypted_content"]);
      assert.equal(initial.parallel_tool_calls, false);
      assert.equal(transport.requests[0].headers.get("Authorization"), "Bearer fixture-key");
      assert.deepEqual(definitions[0].parameters, tool.parameters);
      const input = objectArray(next.input, "input");
      assert.equal(input[1].encrypted_content, "opaque-fixture");
      assert.deepEqual(JSON.parse(String(input[2].arguments)), { text: "café" });
      assert.equal(input[3].output, "café");
    }
  });
}

test("SSE supports CRLF, final unterminated lines, and split UTF-8", async () => {
  for (const anthropic of [false, true]) {
    const source = Buffer.from(fixture(anthropic)).toString();
    for (const wire of [source.replaceAll("\n", "\r\n"), source.trimEnd()])
      assert.equal((await runBytes(Buffer.from(wire), anthropic, 1)).text, "done\ncafé");
  }
});

test("SSE rejects invalid UTF-8, JSON, truncation, and byte limits", async () => {
  const half = "x".repeat(MAX_SSE_BYTES / 2);
  for (const bytes of [
    Buffer.from([0x3a, 0xc3, 0x28, 0x0a]),
    Buffer.from("data: {} {}\n\n"),
    Buffer.from("data: {"),
    Buffer.from("data: [DONE]\n\n"),
    Buffer.from("data: {}\n\n"),
    Buffer.from(":" + "x".repeat(MAX_SSE_BYTES) + "\n"),
    Buffer.from("data: " + half + "\ndata: " + half + "\n\n"),
  ])
    await assert.rejects(runBytes(bytes), ProviderError);
});

test("OpenAI rejects incomplete, malformed, and duplicate calls", async () => {
  const call = { type: "function_call", call_id: "c", name: "echo", arguments: "{}" };
  const invalid: Item[] = [
    { type: "response.failed" },
    { type: "response.completed", response: { status: "incomplete", output: [] } },
    { type: "response.completed", response: { output: [null] } },
    { type: "response.completed", response: { output: [call, call] } },
    { type: "response.output_item.added", output_index: -1, item: call },
    { type: "response.output_text.delta", delta: 3 },
  ];
  for (const arguments_ of ["[]", "{", "", '{"x":NaN}'])
    invalid.push({ type: "response.completed", response: { output: [{ ...call, arguments: arguments_ }] } });
  for (const event of invalid) await assert.rejects(runBytes(events(event)), ProviderError);
});

test("Anthropic rejects truncation, mismatched deltas, and duplicate calls", async () => {
  const original = Buffer.from(fixture(true, true)).toString();
  const duplicate = events(
    {
      type: "content_block_start",
      index: 2,
      content_block: { type: "tool_use", id: "call_1", name: "echo", input: {} },
    },
    { type: "content_block_stop", index: 2 },
  ).toString();
  for (const source of [
    original.replace('"stop_reason":"tool_use"', '"stop_reason":"max_tokens"'),
    original.replace('"stop_reason":"tool_use"', '"stop_reason":"pause_turn"'),
    original.replaceAll('"index":1', '"index":-1'),
    original.replaceAll('"type":"input_json_delta"', '"type":"text_delta"'),
    original.replace('data: {"type":"content_block_stop","index":1}\n\n', ""),
    original.replace('data: {"type":"message_stop"}', duplicate + 'data: {"type":"message_stop"}'),
    original.split('data: {"type":"message_delta"')[0] + 'data: {"type":"message_stop"}\n\n',
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
  ])
    await assert.rejects(runBytes(Buffer.from(source), true), ProviderError);
});

test("tool argument bounds apply across individually bounded events", async () => {
  for (const anthropic of [false, true]) {
    const source = Buffer.from(fixture(anthropic, true)).toString();
    const value = "é".repeat(MAX_SSE_BYTES / 4);
    const delta = events(
      anthropic
        ? { type: "content_block_delta", index: 1, delta: { type: "input_json_delta", partial_json: value } }
        : { type: "response.function_call_arguments.delta", output_index: 1, delta: value },
    ).toString();
    const marker = anthropic
      ? 'data: {"type":"content_block_delta","index":1'
      : 'data: {"type":"response.function_call_arguments.delta"';
    const wire = source.slice(0, source.indexOf(marker)) + delta.repeat(3);
    await assert.rejects(runBytes(Buffer.from(wire), anthropic), /arguments exceed/);
  }
});

test("OpenAI preserves tool arguments with sparse output indices", async () => {
  const transport = new FixtureFetch([
    new FixtureBody(
      events(
        {
          type: "response.output_item.added",
          output_index: 7,
          item: { type: "function_call", call_id: "c", name: "echo", arguments: "" },
        },
        { type: "response.function_call_arguments.delta", output_index: 7, delta: '{"text":"café"}' },
        { type: "response.completed", response: {} },
      ),
    ),
    new FixtureBody(fixture(false)),
  ]);
  const result = await new Agent({
    model: "fixture",
    provider: provider(false, transport),
    tools: [
      new Tool({ name: "echo", description: "Echo", parameters: { type: "object" }, handler: (args) => args.text }),
    ],
  }).run("go");
  assert.equal(result.history[2].output, "café");
  const input = objectArray((await transport.payload(1)).input, "input");
  assert.deepEqual(JSON.parse(String(input[1].arguments)), { text: "café" });
});

test("cancellation and deadlines release pending headers and body reads", async () => {
  for (const anthropic of [false, true]) {
    for (const headers of [false, true]) {
      for (const timeout of [false, true]) {
        const body = new FixtureBody(new Uint8Array(), { hold: true });
        const transport = new FixtureFetch([body], { holdHeaders: headers });
        const controller = new AbortController();
        const run = new Agent({ model: "fixture", provider: provider(anthropic, transport, timeout ? 50 : 2000) }).run(
          "go",
          [],
          { signal: controller.signal },
        );
        const checked = assert.rejects(run, timeout ? /timed out/ : { name: "AbortError" });
        await (headers ? transport.started.promise : body.waiting.promise);
        if (!timeout) controller.abort();
        await checked;
        assert.equal(transport.requests[0].signal.aborted, true);
        if (!headers) {
          assert.equal(body.cancelled, true);
          assert.equal(body.stream.locked, false);
        }
      }
    }
  }
});

test("terminal completion and consumer failure close unfinished bodies", async () => {
  for (const anthropic of [false, true]) {
    for (const consumerFails of [false, true]) {
      const body = new FixtureBody(fixture(anthropic), { hold: true });
      const transport = new FixtureFetch([body]);
      const failure = new Error("consumer stopped");
      const run = new Agent({ model: "fixture", provider: provider(anthropic, transport) }).run("go", [], {
        onEvent(event) {
          if (consumerFails && event.type === "text_delta") throw failure;
        },
      });
      if (consumerFails) await assert.rejects(run, (error) => error === failure);
      else assert.equal((await run).text, "done\ncafé");
      assert.equal(body.cancelled, true);
      assert.equal(body.stream.locked, false);
    }
  }
});

test("backpressure stops body reads while the consumer handles a text delta", async () => {
  const body = new FixtureBody(fixture(false));
  const transport = new FixtureFetch([body]);
  let checked = false;
  await new Agent({ model: "fixture", provider: provider(false, transport) }).run("go", [], {
    async onEvent(event) {
      if (event.type !== "text_delta" || checked) return;
      checked = true;
      const produced = body.produced;
      await nextTick();
      assert.equal(body.produced, produced);
    },
  });
  assert.equal(checked, true);
});

test("cancellation awaits the body's cooperative cleanup", async () => {
  const release = deferred<void>();
  const body = new FixtureBody(new Uint8Array(), { hold: true, cleanup: release.promise });
  const transport = new FixtureFetch([body]);
  const controller = new AbortController();
  let settled = false;
  const run = new Agent({ model: "fixture", provider: provider(false, transport) }).run("go", [], {
    signal: controller.signal,
  });
  const checked = assert.rejects(run, { name: "AbortError" }).finally(() => {
    settled = true;
  });
  try {
    await body.waiting.promise;
    controller.abort();
    await nextTick();
    assert.equal(settled, false);
  } finally {
    release.resolve();
    await checked;
  }
  assert.equal(body.stream.locked, false);
});

test("a deadline rejects buffered completion after a slow text consumer", async () => {
  const bytes = fixture(false);
  const body = new FixtureBody(bytes, { chunkSize: bytes.length, hold: true });
  const transport = new FixtureFetch([body]);
  let delivered = false;
  await assert.rejects(
    new Agent({ model: "fixture", provider: provider(false, transport, 50) }).run("go", [], {
      async onEvent(event) {
        if (event.type === "text_delta") {
          delivered = true;
          await body.stopped.promise;
        }
      },
    }),
    /timed out/,
  );
  assert.equal(delivered, true);
  assert.equal(body.stream.locked, false);
});

test("error response bodies are bounded and body-read failures reject the run", async () => {
  const body = new FixtureBody(Buffer.from("denied".repeat(10_000)), { chunkSize: 8192 });
  const transport = new FixtureFetch([body], { status: 401 });
  await assert.rejects(new Agent({ model: "fixture", provider: provider(false, transport) }).run("go"), (error) => {
    assert.ok(error instanceof ProviderError);
    assert.match(error.message, /API error 401/);
    assert.ok(error.message.length < 8300);
    return true;
  });
  assert.ok(body.produced <= 8192);
  assert.equal(body.cancelled, true);
  const broken = new FixtureBody(new Uint8Array(), { failure: new Error("body failed") });
  await assert.rejects(
    new Agent({ model: "fixture", provider: provider(false, new FixtureFetch([broken])) }).run("go"),
    /body failed/,
  );
  assert.equal(broken.stream.locked, false);
});

test("credentials are resolved at request time and pre-cancelled requests do no I/O", async () => {
  for (const anthropic of [false, true]) {
    const variable = anthropic ? "ANTHROPIC_API_KEY" : "OPENAI_API_KEY";
    const previous = process.env[variable];
    delete process.env[variable];
    try {
      const transport = new FixtureFetch([new FixtureBody(fixture(anthropic))]);
      const options = { fetch: transport.fetch, endpoint: "http://fixture.invalid" };
      const adapter = anthropic ? new AnthropicProvider(options) : new OpenAIProvider(options);
      const agent = new Agent({ model: "fixture", provider: adapter });
      await assert.rejects(agent.run("go"), new RegExp(variable));
      const controller = new AbortController();
      controller.abort();
      await assert.rejects(agent.run("go", [], { signal: controller.signal }), { name: "AbortError" });
      assert.equal(transport.requests.length, 0);
      process.env[variable] = "late-key";
      assert.equal((await agent.run("go")).text, "done\ncafé");
      assert.ok(transport.requests[0].headers.get(anthropic ? "x-api-key" : "Authorization")?.includes("late-key"));
    } finally {
      if (previous === undefined) delete process.env[variable];
      else process.env[variable] = previous;
    }
  }
});
