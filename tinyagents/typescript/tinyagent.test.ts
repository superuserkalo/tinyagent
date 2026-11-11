import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { Readable, Writable } from "node:stream";
import test from "node:test";
import {
  Agent,
  AnthropicProvider,
  MAX_HTTP_BODY_BYTES,
  MAX_JSONL_LINE_BYTES,
  MAX_QUEUED_OUTPUT_BYTES,
  MAX_TOOL_OUTPUT_BYTES,
  OpenAIProvider,
  PROTOCOL_VERSION,
  ProviderError,
  RpcSession,
  Tool,
  runBash,
  runRpc,
  type AgentEvent,
  type Item,
  type OpenAIEventTransport,
  type Provider,
} from "./tiny_agent.ts";

function providerFromTurns(turns: Item[][]): OpenAIProvider {
  const remaining = [...turns];
  const eventTransport: OpenAIEventTransport = async function* () {
    for (const event of remaining.shift() ?? []) yield event;
  };
  return new OpenAIProvider({ eventTransport });
}

function messageEvents(...deltas: string[]): Item[] {
  const text = deltas.join("");
  const item = {
    type: "message",
    id: "msg_1",
    role: "assistant",
    content: [{ type: "output_text", text }],
  };
  return [
    { type: "response.output_item.added", output_index: 0, item: { ...item, content: [] } },
    ...deltas.map((delta) => ({ type: "response.output_text.delta", output_index: 0, delta })),
    { type: "response.output_item.done", output_index: 0, item },
    { type: "response.completed", response: {} },
  ];
}

interface RpcFixture {
  protocolVersion: number;
  limits: Record<string, number>;
  scenarios: { commands: Item[]; expected: Item[] }[];
}

const rpcFixture = JSON.parse(
  readFileSync(new URL("../../conformance/rpc_v1.json", import.meta.url), "utf8"),
) as RpcFixture;

function terminalStatus(events: AgentEvent[]): string | undefined {
  const terminal = events.at(-1);
  return terminal?.type === "agent_end" ? terminal.status : undefined;
}

function jsonObject(text: string): Item {
  const value: unknown = JSON.parse(text);
  assert.ok(typeof value === "object" && value !== null && !Array.isArray(value));
  return value as Item;
}

async function captureRpc(input: Buffer): Promise<Item[]> {
  const chunks: Buffer[] = [];
  const output = new Writable({
    write(chunk, _encoding, callback) {
      chunks.push(Buffer.from(chunk));
      callback();
    },
  });
  await runRpc(new Agent({ model: "test", transport: () => [] }), Readable.from([input]), output);
  return Buffer.concat(chunks).toString("utf8").trim().split("\n").filter(Boolean).map(jsonObject);
}

test("shared limits and static JSONL fixtures", async () => {
  // conformance/rpc_v1.json is the version 1 fixture. Version 2 only adds to it,
  // so its expectations are brought up to date here: get_state gained three fields.
  assert.ok(rpcFixture.protocolVersion <= PROTOCOL_VERSION);
  for (const scenario of rpcFixture.scenarios)
    for (const expected of scenario.expected ?? [])
      if (expected.command === "get_state" && expected.success)
        expected.data = { ...(expected.data as Item), protocolVersion: PROTOCOL_VERSION, resumable: false, paused: null, usage: null };
  assert.deepEqual(rpcFixture.limits, {
    jsonlLineBytes: MAX_JSONL_LINE_BYTES,
    httpBodyBytes: MAX_HTTP_BODY_BYTES,
    queuedOutputBytes: MAX_QUEUED_OUTPUT_BYTES,
    toolOutputBytes: MAX_TOOL_OUTPUT_BYTES,
  });
  const scenarios = rpcFixture.scenarios.slice(0, 6);
  const commands = scenarios.flatMap((scenario) => scenario.commands);
  const input = Buffer.from(`${commands.map((command: Item) => JSON.stringify(command)).join("\n")}\n{\n`);
  const actual = await captureRpc(input);
  const expected = scenarios.flatMap((scenario) => scenario.expected);
  expected.push({
    type: "protocol_error",
    error: { code: "invalid_json", message: "RPC input is not valid JSON" },
  });
  assert.deepEqual(actual, expected);
});

async function invokeRpcCli(input: Buffer): Promise<{ stdout: Buffer; stderr: Buffer }> {
  // The CLI stores sessions under the working directory unless told otherwise.
  const sessions = mkdtempSync(join(tmpdir(), "tinyagent-cli-"));
  const child = spawn(process.execPath, [fileURLToPath(new URL("./tiny_agent.ts", import.meta.url)), "--mode", "rpc"], {
    env: { ...process.env, OPENAI_API_KEY: "test", TINYAGENT_PROVIDER: "openai", TINYAGENT_SESSION_DIR: sessions },
    stdio: ["pipe", "pipe", "pipe"],
  });
  const stdout: Buffer[] = [];
  const stderr: Buffer[] = [];
  child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk));
  child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk));
  child.stdin.end(input);
  let code: number | null;
  try {
    code = await new Promise<number | null>((resolve, reject) => {
      child.once("error", reject);
      child.once("close", resolve);
    });
  } finally {
    rmSync(sessions, { recursive: true });
  }
  assert.equal(code, 0);
  return { stdout: Buffer.concat(stdout), stderr: Buffer.concat(stderr) };
}

test("RPC CLI subprocess preserves framing and rejects fatal input", async () => {
  const completed = await invokeRpcCli(Buffer.from('{"id":"state","type":"get_state"}\n'));
  const lines = completed.stdout.toString("utf8").trim().split("\n");
  assert.equal(lines.length, 1);
  assert.equal((jsonObject(lines[0]).data as Item).status, "idle");
  // Node 22 announces node:sqlite, which locks durable sessions, once on stderr.
  const warning = /^\(node:\d+\) ExperimentalWarning: SQLite is an experimental feature.*\n\(Use `node --trace-warnings.*\n/;
  assert.equal(completed.stderr.toString("utf8").replace(warning, ""), "");
  const oversized = await invokeRpcCli(Buffer.alloc(MAX_JSONL_LINE_BYTES + 1, 0x20));
  assert.equal((jsonObject(oversized.stdout.toString()).error as Item).code, "line_too_large");
  const invalid = await invokeRpcCli(Buffer.from([0xff, 0x0a]));
  assert.equal((jsonObject(invalid.stdout.toString()).error as Item).code, "invalid_utf8");
});

test("the CLI runs through a symlink, as an installed command does", () => {
  const directory = mkdtempSync(join(tmpdir(), "tinyagent-cli-"));
  try {
    const command = join(directory, "tinyagent.ts");
    symlinkSync(fileURLToPath(new URL("./tiny_agent.ts", import.meta.url)), command);
    const completed = spawnSync(process.execPath, [command, "--self-test"], { encoding: "utf8" });
    assert.equal(completed.status, 0, completed.stderr);
    assert.equal(completed.stdout, "self-test passed\n");
  } finally {
    rmSync(directory, { recursive: true });
  }
});

test("RPC busy rejection, abort, and rollback", async () => {
  let markStarted: () => void = () => {};
  const started = new Promise<void>((resolve) => (markStarted = resolve));
  const provider: Provider = {
    async *stream(request) {
      yield { type: "text_delta", delta: "waiting" };
      markStarted();
      await new Promise<void>((resolve) => request.signal.addEventListener("abort", () => resolve(), { once: true }));
      const error = new Error("run aborted");
      error.name = "AbortError";
      throw error;
    },
  };
  const emitted: Item[] = [];
  const session = new RpcSession(new Agent({ model: "test", provider }), async (value) => {
    emitted.push(structuredClone(value) as Item);
  });
  await session.handle({ id: "first", type: "prompt", message: "wait" });
  await started;
  await session.handle({ id: "second", type: "prompt", message: "busy" });
  await session.handle({ id: "abort", type: "abort" });
  assert.equal(await session.waitIdle(1_000), true);
  await session.handle({ id: "state", type: "get_state" });

  assert.equal(emitted[0].command, "prompt");
  const runId = (emitted[0].data as Item).runId;
  assert.equal(emitted[1].type, "agent_start");
  assert.equal(emitted[1].runId, runId);
  const busy = emitted.find((item) => item.id === "second") as Item;
  assert.equal((busy.error as Item).code, "busy");
  const abort = emitted.find((item) => item.id === "abort") as Item;
  assert.deepEqual(abort.data, { accepted: true, runId });
  assert.equal(emitted.find((item) => item.type === "agent_end")?.status, "aborted");
  assert.deepEqual(emitted.at(-1)?.data, {
    protocolVersion: 2,
    status: "idle",
    runId: null,
    messageCount: 0,
    resumable: false,
    paused: null,
    usage: { inputTokens: 0, outputTokens: 0, requests: 0 },
  });
});

test("RPC commits completed history and rolls failed history back", async () => {
  let turn = 0;
  const provider: Provider = {
    async *stream() {
      turn += 1;
      if (turn === 2) {
        yield { type: "text_delta", delta: "partial" };
        throw new ProviderError("failed");
      }
      const text = `answer ${turn}`;
      return {
        text,
        assistant: { role: "assistant", content: [{ type: "text", text }] },
        output: [],
        toolCalls: [],
      };
    },
  };
  const emitted: Item[] = [];
  const session = new RpcSession(
    new Agent({ model: "test", provider }),
    (value) => {
      emitted.push(value as Item);
    },
    () => {},
  );
  await session.handle({ id: "one", type: "prompt", message: "first" });
  assert.equal(await session.waitIdle(1_000), true);
  await session.handle({ id: "state1", type: "get_state" });
  assert.equal((emitted.at(-1)?.data as Item).messageCount, 2);
  await session.handle({ id: "two", type: "prompt", message: "second" });
  assert.equal(await session.waitIdle(1_000), true);
  await session.handle({ id: "state2", type: "get_state" });
  assert.equal((emitted.at(-1)?.data as Item).messageCount, 2);
  assert.equal(emitted.filter((item) => item.type === "agent_end" && item.status === "failed").length, 1);
  await session.handle({ id: "new", type: "new_session" });
  await session.handle({ id: "state3", type: "get_state" });
  assert.equal((emitted.at(-1)?.data as Item).messageCount, 0);
});

test("RPC new_session aborts an active run and clears history", async () => {
  let markStarted: () => void = () => {};
  const started = new Promise<void>((resolve) => (markStarted = resolve));
  const provider: Provider = {
    async *stream(request) {
      markStarted();
      if (!request.signal.aborted) {
        await new Promise<void>((resolve) => request.signal.addEventListener("abort", () => resolve(), { once: true }));
      }
      const error = new Error("run aborted");
      error.name = "AbortError";
      throw error;
    },
  };
  const emitted: Item[] = [];
  const session = new RpcSession(new Agent({ model: "test", provider }), (value) => {
    emitted.push(value as Item);
  });
  await session.handle({ id: "prompt", type: "prompt", message: "wait" });
  await started;
  await session.handle({ id: "new", type: "new_session" });
  await session.handle({ id: "state", type: "get_state" });
  assert.deepEqual(emitted.find((item) => item.id === "new")?.data, { abortedRun: true });
  assert.equal(emitted.find((item) => item.type === "agent_end")?.status, "aborted");
  assert.equal((emitted.at(-1)?.data as Item).messageCount, 0);
});

test("RPC EOF cancels an active run cleanly", async () => {
  const provider: Provider = {
    async *stream(request) {
      if (!request.signal.aborted) {
        await new Promise<void>((resolve) => request.signal.addEventListener("abort", () => resolve(), { once: true }));
      }
      const error = new Error("run aborted");
      error.name = "AbortError";
      throw error;
    },
  };
  const chunks: Buffer[] = [];
  const output = new Writable({
    write(chunk, _encoding, callback) {
      chunks.push(Buffer.from(chunk));
      callback();
    },
  });
  await runRpc(
    new Agent({ model: "test", provider }),
    Readable.from([Buffer.from('{"id":"prompt","type":"prompt","message":"wait"}\n')]),
    output,
  );
  const messages = Buffer.concat(chunks).toString("utf8").trim().split("\n").map(jsonObject);
  assert.equal(messages[0].command, "prompt");
  assert.equal(messages.at(-1)?.status, "aborted");
});

test("RPC oversized output is structured and cancels the run", async () => {
  let signalOutput: () => void = () => {};
  const outputSeen = new Promise<void>((resolve) => (signalOutput = resolve));
  const provider: Provider = {
    async *stream() {
      yield { type: "text_delta", delta: "x".repeat(MAX_JSONL_LINE_BYTES) };
      throw new Error("oversized output should cancel the run");
    },
  };
  const chunks: Buffer[] = [];
  const output = new Writable({
    write(chunk, _encoding, callback) {
      const copy = Buffer.from(chunk);
      chunks.push(copy);
      if (copy.includes(Buffer.from('"code":"output_too_large"'))) signalOutput();
      callback();
    },
  });
  const input = Readable.from(
    (async function* () {
      yield Buffer.from('{"id":"prompt","type":"prompt","message":"large"}\n');
      await outputSeen;
    })(),
  );
  await runRpc(new Agent({ model: "test", provider }), input, output);
  const messages = Buffer.concat(chunks).toString("utf8").trim().split("\n").map(jsonObject);
  assert.equal(messages[0].command, "prompt");
  assert.ok(messages.some((message) => (message.error as Item | undefined)?.code === "output_too_large"));
});

test("RPC rejects fatal oversized and invalid UTF-8 input", async () => {
  const oversized = await captureRpc(Buffer.alloc(MAX_JSONL_LINE_BYTES + 1, 0x20));
  assert.equal((oversized[0].error as Item).code, "line_too_large");
  const invalid = await captureRpc(Buffer.from([0xff, 0x0a]));
  assert.equal((invalid[0].error as Item).code, "invalid_utf8");
});

test("tool output limit is a recoverable tool error", async () => {
  const responses: Item[][] = [
    [{ type: "function_call", call_id: "large", name: "large", arguments: "{}" }],
    [{ type: "message", content: [{ type: "output_text", text: "recovered" }] }],
  ];
  const emitted: AgentEvent[] = [];
  const result = await new Agent({
    model: "test",
    tools: [
      new Tool({
        name: "large",
        description: "Large output.",
        parameters: { type: "object" },
        handler: () => "x".repeat(MAX_TOOL_OUTPUT_BYTES + 1),
      }),
    ],
    transport: () => responses.shift() ?? [],
  }).run("go", [], {
    onEvent: (event) => {
      emitted.push(event);
    },
  });
  const toolEnd = emitted.find((event) => event.type === "tool_execution_end");
  assert.equal(toolEnd?.type === "tool_execution_end" && toolEnd.isError, true);
  assert.match(toolEnd?.type === "tool_execution_end" ? String(toolEnd.output) : "", /ToolOutputLimitError/);
  assert.ok((toolEnd?.type === "tool_execution_end" ? toolEnd.output.length : 1_000) < 200);
  assert.equal(result.text, "recovered");
});

test("D18 bash output of 100,000 bytes keeps its head and tail around an elided line", async () => {
  // Each line is 10 bytes, so the cut points are known. The process finishes normally: nothing kills it.
  const output = await runBash("for i in $(seq 1 10000); do printf '%09d\\n' $i; done; echo done >&2");
  const lines = (first: number, last: number) =>
    Array.from({ length: last - first + 1 }, (_, index) => `${String(first + index).padStart(9, "0")}\n`).join("");
  assert.equal(output, `exit_code: 0\nstdout:\n${lines(1, 1500)}\n[... 70000 bytes elided ...]\n${lines(8501, 10000)}stderr:\ndone\n`);
  // A multi-byte character is never cut in half.
  const snowmen = await runBash("{ printf xy; yes ☃ | head -c 40000; }");
  assert.doesNotMatch(snowmen, /\uFFFD/);
  assert.match(snowmen, /\n\[\.\.\. 1000[0-9] bytes elided \.\.\.\]\n/);
  assert.match(await runBash("printf '%030000d' 7"), /^exit_code: 0\nstdout:\n0{29999}7stderr:\n$/);
});

test("compatible complete transport tool loop", async () => {
  const responses: Item[][] = [
    [
      { type: "reasoning", id: "reasoning_1", summary: [] },
      {
        type: "function_call",
        call_id: "call_1",
        name: "echo",
        arguments: '{"text":"hello"}',
      },
    ],
    [
      {
        type: "message",
        content: [{ type: "output_text", text: "Done." }],
      },
    ],
  ];
  const requests: Item[] = [];
  const echo = new Tool({
    name: "echo",
    description: "Return text.",
    parameters: { type: "object" },
    handler: ({ text }) => text,
  });
  const agent = new Agent({
    model: "test",
    tools: [echo],
    transport(payload) {
      requests.push(structuredClone(payload));
      return responses.shift() ?? [];
    },
  });

  const result = await agent.run("Echo hello");
  assert.equal(result.text, "Done.");
  assert.deepEqual(
    result.history.map((item) => item.role),
    ["user", "assistant", "tool_result", "assistant"],
  );
  assert.equal(result.history.at(-2)?.callId, "call_1");
  assert.equal(result.history.at(-2)?.output, "hello");
  const finalInput = requests.at(-1)?.input as Item[];
  assert.deepEqual(
    finalInput.map((item) => item.type ?? item.role),
    ["user", "reasoning", "function_call", "function_call_output"],
  );
});

test("text deltas, buffered arguments, sequential tools, and tool failure", async () => {
  const functionEvents: Item[] = [];
  for (const [index, callId, text] of [
    [0, "call_1", "one"],
    [1, "call_2", "two"],
  ] as const) {
    functionEvents.push(
      {
        type: "response.output_item.added",
        output_index: index,
        item: { type: "function_call", call_id: callId, name: "echo", arguments: "" },
      },
      { type: "response.function_call_arguments.delta", output_index: index, delta: '{"text":' },
      { type: "response.function_call_arguments.delta", output_index: index, delta: `"${text}"}` },
      {
        type: "response.output_item.done",
        output_index: index,
        item: { type: "function_call", call_id: callId, name: "echo", arguments: "" },
      },
    );
  }
  functionEvents.push({ type: "response.completed", response: {} });
  const calls: string[] = [];
  const echo = new Tool({
    name: "echo",
    description: "Return text.",
    parameters: { type: "object" },
    handler({ text }, signal) {
      assert.equal(signal.aborted, false);
      assert.ok(typeof text === "string");
      calls.push(text);
      if (text === "two") throw new Error("boom");
      return text;
    },
  });
  const emitted: AgentEvent[] = [];
  const result = await new Agent({
    model: "test",
    provider: providerFromTurns([functionEvents, messageEvents("Do", "ne.")]),
    tools: [echo],
  }).run("go", [], {
    onEvent: (event) => {
      emitted.push(event);
    },
  });

  assert.equal(result.text, "Done.");
  assert.deepEqual(calls, ["one", "two"]);
  assert.deepEqual(
    emitted.map((event) => event.type),
    [
      "agent_start",
      "tool_execution_start",
      "tool_execution_end",
      "tool_execution_start",
      "tool_execution_end",
      "text_delta",
      "text_delta",
      "agent_end",
    ],
  );
  const toolEnds = emitted.filter((event) => event.type === "tool_execution_end");
  assert.deepEqual(
    toolEnds.map((event) => event.isError),
    [false, true],
  );
  assert.match(String(result.history.at(-2)?.output), /Error: boom/);
  assert.deepEqual(
    emitted.map((event) => event.sequence),
    emitted.map((_, index) => index + 1),
  );
  assert.equal(new Set(emitted.map((event) => event.runId)).size, 1);
});

test("malformed arguments emit a failed terminal event", async () => {
  const provider = providerFromTurns([
    [
      {
        type: "response.output_item.added",
        output_index: 0,
        item: { type: "function_call", call_id: "c", name: "echo", arguments: "" },
      },
      { type: "response.function_call_arguments.delta", output_index: 0, delta: '{"text":' },
      {
        type: "response.output_item.done",
        output_index: 0,
        item: { type: "function_call", call_id: "c", name: "echo", arguments: "" },
      },
      { type: "response.completed", response: {} },
    ],
  ]);
  const emitted: AgentEvent[] = [];
  await assert.rejects(
    new Agent({ model: "test", provider }).run("go", [], {
      onEvent: (event) => {
        emitted.push(event);
      },
    }),
    (error) => error instanceof ProviderError && /invalid arguments/.test(error.message),
  );
  assert.deepEqual(
    emitted.map((event) => event.type),
    ["agent_start", "agent_end"],
  );
  assert.equal(terminalStatus(emitted), "failed");
});

test("provider failures emit a failed terminal event", async () => {
  const provider = providerFromTurns([
    [
      { type: "response.output_text.delta", output_index: 0, delta: "partial" },
      { type: "response.failed", response: { error: { message: "upstream exploded" } } },
    ],
  ]);
  const emitted: AgentEvent[] = [];
  await assert.rejects(
    new Agent({ model: "test", provider }).run("go", [], {
      onEvent: (event) => {
        emitted.push(event);
      },
    }),
    /upstream exploded/,
  );
  assert.deepEqual(
    emitted.map((event) => event.type),
    ["agent_start", "text_delta", "agent_end"],
  );
  assert.equal(terminalStatus(emitted), "failed");
});

test("run-scoped cancellation emits an aborted terminal event", async () => {
  const controller = new AbortController();
  const eventTransport: OpenAIEventTransport = async function* () {
    yield { type: "response.output_text.delta", output_index: 0, delta: "partial" };
    controller.abort();
    yield { type: "response.completed", response: { output: [] } };
  };
  const emitted: AgentEvent[] = [];
  await assert.rejects(
    new Agent({ model: "test", provider: new OpenAIProvider({ eventTransport }) }).run("go", [], {
      signal: controller.signal,
      onEvent: (event) => {
        emitted.push(event);
      },
    }),
    (error) => error instanceof Error && error.name === "AbortError",
  );
  assert.deepEqual(
    emitted.map((event) => event.type),
    ["agent_start", "text_delta", "agent_end"],
  );
  assert.equal(terminalStatus(emitted), "aborted");
});

test("Anthropic streaming tools and lossless replay", async () => {
  const turns: Item[][] = [
    [
      {
        type: "message_start",
        message: {
          id: "msg_tool",
          model: "claude-test",
          role: "assistant",
          content: [],
          usage: { input_tokens: 4 },
        },
      },
      {
        type: "content_block_start",
        index: 0,
        content_block: { type: "thinking", thinking: "", signature: "" },
      },
      { type: "content_block_delta", index: 0, delta: { type: "thinking_delta", thinking: "Use echo." } },
      { type: "content_block_delta", index: 0, delta: { type: "signature_delta", signature: "sig_123" } },
      { type: "content_block_stop", index: 0 },
      {
        type: "content_block_start",
        index: 1,
        content_block: { type: "tool_use", id: "tool_1", name: "echo", input: {} },
      },
      { type: "content_block_delta", index: 1, delta: { type: "input_json_delta", partial_json: '{"text":' } },
      { type: "content_block_delta", index: 1, delta: { type: "input_json_delta", partial_json: '"hello"}' } },
      { type: "content_block_stop", index: 1 },
      { type: "message_delta", delta: { stop_reason: "tool_use" }, usage: { output_tokens: 8 } },
      { type: "message_stop" },
    ],
    [
      { type: "message_start", message: { id: "msg_text", model: "claude-test", usage: { input_tokens: 9 } } },
      { type: "content_block_start", index: 0, content_block: { type: "text", text: "" } },
      { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "Done." } },
      { type: "content_block_stop", index: 0 },
      { type: "message_delta", delta: { stop_reason: "end_turn" }, usage: { output_tokens: 2 } },
      { type: "message_stop" },
    ],
  ];
  const requests: Item[] = [];
  const eventTransport: OpenAIEventTransport = async function* (payload, signal) {
    assert.equal(signal.aborted, false);
    requests.push(structuredClone(payload));
    for (const event of turns.shift() ?? []) yield event;
  };
  const echo = new Tool({
    name: "echo",
    description: "Return text.",
    parameters: { type: "object" },
    handler: ({ text }) => text,
  });
  const emitted: AgentEvent[] = [];
  const result = await new Agent({
    model: "claude-test",
    instructions: "Be concise.",
    tools: [echo],
    provider: new AnthropicProvider({ eventTransport, maxTokens: 123 }),
  }).run("Echo hello", [], {
    onEvent: (event) => {
      emitted.push(event);
    },
  });

  assert.equal(result.text, "Done.");
  assert.deepEqual(
    emitted.map((event) => event.type),
    ["agent_start", "tool_execution_start", "tool_execution_end", "text_delta", "agent_end"],
  );
  assert.equal(requests[0].system, "Be concise.");
  assert.equal(requests[0].max_tokens, 123);
  assert.deepEqual((requests[0].tools as Item[])[0].input_schema, { type: "object" });
  const replay = requests[1].messages as Item[];
  assert.deepEqual(
    replay.map((message) => message.role),
    ["user", "assistant", "user"],
  );
  assert.deepEqual((replay[1].content as Item[])[0], {
    type: "thinking",
    thinking: "Use echo.",
    signature: "sig_123",
  });
  assert.deepEqual((replay[1].content as Item[])[1].input, { text: "hello" });
  assert.deepEqual((replay[2].content as Item[])[0], {
    type: "tool_result",
    tool_use_id: "tool_1",
    content: "hello",
    is_error: false,
  });
  const metadata = result.history[1].providerMetadata as Item;
  assert.equal(metadata.provider, "anthropic-messages");
  assert.deepEqual((metadata.message as Item).usage, { input_tokens: 4, output_tokens: 8 });
});

test("Anthropic malformed arguments and provider errors fail the run", async () => {
  const malformed: Item[] = [
    { type: "message_start", message: { id: "m", model: "claude-test" } },
    {
      type: "content_block_start",
      index: 0,
      content_block: { type: "tool_use", id: "t", name: "echo", input: {} },
    },
    { type: "content_block_delta", index: 0, delta: { type: "input_json_delta", partial_json: '{"text":' } },
    { type: "content_block_stop", index: 0 },
  ];
  const malformedTransport: OpenAIEventTransport = async function* () {
    for (const event of malformed) yield event;
  };
  const emitted: AgentEvent[] = [];
  await assert.rejects(
    new Agent({
      model: "claude-test",
      provider: new AnthropicProvider({ eventTransport: malformedTransport }),
    }).run("go", [], {
      onEvent: (event) => {
        emitted.push(event);
      },
    }),
    (error) => error instanceof ProviderError && /invalid arguments/.test(error.message),
  );
  assert.equal(terminalStatus(emitted), "failed");

  const failedTransport: OpenAIEventTransport = async function* () {
    yield { type: "error", error: { type: "overloaded_error", message: "overloaded" } };
  };
  await assert.rejects(
    new Agent({
      model: "claude-test",
      provider: new AnthropicProvider({ eventTransport: failedTransport }),
    }).run("go"),
    /overloaded/,
  );
});

test("Anthropic streaming observes run cancellation", async () => {
  const controller = new AbortController();
  const eventTransport: OpenAIEventTransport = async function* () {
    yield { type: "message_start", message: { id: "m", content: [] } };
    yield { type: "content_block_start", index: 0, content_block: { type: "text", text: "" } };
    yield { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "partial" } };
    controller.abort();
    yield { type: "content_block_stop", index: 0 };
  };
  const emitted: AgentEvent[] = [];
  await assert.rejects(
    new Agent({
      model: "claude-test",
      provider: new AnthropicProvider({ eventTransport }),
    }).run("go", [], {
      signal: controller.signal,
      onEvent: (event) => {
        emitted.push(event);
      },
    }),
    (error) => error instanceof Error && error.name === "AbortError",
  );
  assert.deepEqual(
    emitted.map((event) => event.type),
    ["agent_start", "text_delta", "agent_end"],
  );
  assert.equal(terminalStatus(emitted), "aborted");
});

test("bash process is cancelled", async () => {
  const controller = new AbortController();
  const started = Date.now();
  setTimeout(() => controller.abort(), 50);
  await assert.rejects(
    runBash("sleep 5", controller.signal),
    (error) => error instanceof Error && error.name === "AbortError",
  );
  assert.ok(Date.now() - started < 2_000);
});
