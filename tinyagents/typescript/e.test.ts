/** Protocol version 2, Part E: tests titled by conformance scenario number. */
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { HttpFixture, body, json, object } from "./http_fixture.ts";
import { Agent, AnthropicProvider, ChatCompletionsProvider, MemoryStorage, OpenAIProvider, RpcSession, RunError, ScriptedProvider, Session, Tool,
  pause, type AgentEvent, type RunResult, type Item, type ScriptedTurn, type ToolContext } from "./tiny_agent.ts";

function call(name: string, arguments_: Item = {}, callId = `call_${name}`): ScriptedTurn {
  return [{ callId, name, arguments: arguments_ }];
}

function tool(name: string, handler: Tool["handler"], extra: Partial<ConstructorParameters<typeof Tool>[0]> = {}): Tool {
  return new Tool({ name, description: name, parameters: { type: "object", properties: { text: { type: "string" } } },
    handler, ...extra });
}

function sse(...events: Item[]): string {
  return events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("");
}

test("D25 baseUrl, headers and extra reach the request; a run's extra overrides the provider's", async () => {
  const seen: { url: string; headers: Headers; body: Item }[] = [];
  const respond = (text: string) => async (input: string | URL | Request, init?: RequestInit) => {
    seen.push({ url: String(input), headers: new Headers(init?.headers), body: JSON.parse(String(init?.body)) as Item });
    return new Response(text);
  };
  const openai = new OpenAIProvider({ baseUrl: "https://gateway.example/v1/", extra: { temperature: 0.2, store: true, top_p: 0.5 },
    headers: { authorization: "Bearer from-header", "X-Team": "search" },
    fetch: respond(sse({ type: "response.completed", response: { output: [] } })) });
  await new Agent({ model: "agent-model", provider: openai }).run("go", [], { model: "run-model", extra: { temperature: 0.9 } });
  assert.equal(seen[0].url, "https://gateway.example/v1/responses");
  assert.deepEqual([seen[0].headers.get("authorization"), seen[0].headers.get("x-team")], ["Bearer from-header", "search"]);
  // extra comes after TinyAgent's own fields; the run's comes after the provider's.
  assert.deepEqual([seen[0].body.model, seen[0].body.temperature, seen[0].body.top_p, seen[0].body.store, seen[0].body.stream],
    ["run-model", 0.9, 0.5, true, true]);
  const anthropic = new AnthropicProvider({ apiKey: "key", baseUrl: "https://gateway.example/anthropic/v1",
    headers: { "anthropic-beta": "feature" }, extra: { max_tokens: 99, thinking: { type: "enabled" } },
    fetch: respond(sse({ type: "message_start", message: { id: "m" } }, { type: "message_delta", delta: { stop_reason: "end_turn" } },
      { type: "message_stop" })) });
  await new Agent({ model: "claude", instructions: "agent instructions", provider: anthropic })
    .run("go", [], { instructions: "run instructions", extra: { thinking: { type: "disabled" } } });
  assert.equal(seen[1].url, "https://gateway.example/anthropic/v1/messages");
  assert.deepEqual([seen[1].headers.get("x-api-key"), seen[1].headers.get("anthropic-beta")], ["key", "feature"]);
  assert.deepEqual([seen[1].body.max_tokens, seen[1].body.thinking, seen[1].body.system], [99, { type: "disabled" }, "run instructions"]);
});

test("D27 beforeTool can deny or replace arguments, afterTool can replace the result, a throwing hook fails the run", async () => {
  const handled: Item[] = [];
  const echo = tool("echo", (arguments_) => { handled.push(arguments_); return `echo ${String(arguments_.text)}`; });
  const provider = new ScriptedProvider([call("echo", { text: "deny me" }), call("echo", { text: "rewrite me" }),
    call("echo", { text: "bad rewrite" }), call("echo", { text: "plain" }), "done"]);
  const contexts: ToolContext[] = [];
  const agent = new Agent({ model: "scripted", provider, tools: [echo],
    beforeTool: (toolCall, context) => {
      contexts.push(context);
      const text = String(toolCall.arguments.text);
      if (text === "deny me") return { deny: "Not allowed: policy 7" };
      if (text === "rewrite me") return { arguments: { text: "rewritten" } };
      if (text === "bad rewrite") return { arguments: { text: 42 } };
      return undefined;
    },
    afterTool: (toolCall, result) => toolCall.arguments.text === "plain" ? `wrapped(${String(result.output)})` : undefined });
  await agent.run("go");
  const observations = provider.requests.slice(1).map((request) => [request.history.at(-1)?.isError, request.history.at(-1)?.output]);
  assert.deepEqual(observations[0], [true, "Not allowed: policy 7"]);
  assert.deepEqual(observations[1], [false, "echo rewritten"]);
  assert.equal(observations[2][0], true);
  assert.match(String(observations[2][1]), /Invalid arguments for echo\/text/, "replaced arguments are validated again");
  assert.deepEqual(observations[3], [false, "wrapped(echo plain)"]);
  assert.deepEqual(handled, [{ text: "rewritten" }, { text: "plain" }]);
  assert.equal(contexts[0].executionId, "call_echo");
  // Hooks see every tool, and an exception in one is the run's failure, not the model's problem.
  for (const hooks of [{ beforeTool: () => { throw new Error("hook broke"); } }, { afterTool: () => { throw new Error("hook broke"); } }]) {
    const session = new Session(new Agent({ model: "scripted", tools: [echo], provider: new ScriptedProvider([call("echo"), "never"]), ...hooks }));
    const events: AgentEvent[] = [];
    await assert.rejects(session.run("go", { onEvent: (event) => { events.push(event); } }),
      (error) => error instanceof RunError && error.code === "hook_error" && /hook broke/.test(error.message));
    const end = events.at(-1);
    assert.ok(end?.type === "agent_end" && end.status === "failed" && end.error.code === "hook_error");
    assert.deepEqual(events.map((event) => event.type), ["agent_start", "tool_execution_start", "tool_execution_end", "agent_end"]);
    assert.equal(session.recoveryRequired, false);
  }
});

test("D28 pause from a handler and from beforeTool; resume with a result or an error, also after reopening", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "ta-e-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  let deploys = 0;
  const build = (turns: ScriptedTurn[], onUncertain?: "reconcile") => {
    const provider = new ScriptedProvider(turns);
    const agent = new Agent({ model: "scripted", provider, tools: [
      tool("ask", ({ text }) => pause({ question: text })),
      tool("deploy", () => { deploys += 1; return "deployed"; }),
    ], beforeTool: (toolCall) => toolCall.name === "deploy" && deploys === 0 ? pause({ approve: "deploy" }) : undefined });
    return { provider, agent, options: { directory, onUncertain } };
  };
  const first = build([call("ask", { text: "Which region?" })]);
  const session = new Session(first.agent, first.options);
  const events: AgentEvent[] = [];
  const result = await session.run("ship it", { onEvent: (event) => { events.push(event); } });
  const expected = { toolCallId: "call_ask", toolName: "ask", payload: { question: "Which region?" } };
  assert.deepEqual(result.pause, expected);
  // G21: the call that paused still gets its end, marked paused, before the run's paused end.
  assert.deepEqual(events.map((event) => event.type), ["agent_start", "tool_execution_start", "tool_execution_end", "agent_end"]);
  const toolEnd = events[2];
  assert.ok(toolEnd.type === "tool_execution_end");
  assert.deepEqual([toolEnd.paused, toolEnd.output, toolEnd.isError], [true, "", false]);
  const end = events.at(-1);
  assert.ok(end?.type === "agent_end" && end.status === "paused");
  assert.deepEqual(end.pause, expected);
  assert.deepEqual([session.paused, session.resumable, session.recoveryRequired], [expected, true, false]);
  assert.deepEqual(session.history, [], "a paused run has committed nothing");
  assert.deepEqual(session.records.slice(-2).map((record) => record.type), ["tool_paused", "run_paused"]);
  await assert.rejects(session.run("something else"), { code: "resume_required" });
  await assert.rejects(session.resume(), /paused/);
  await session.close();

  // Reopened by another Session object over the same files, as a new process would; strict recovery does not apply.
  const second = build([call("deploy"), "never reached"], "reconcile");
  const reopened = new Session(second.agent, { ...second.options, sessionId: session.id, resume: true });
  assert.deepEqual([reopened.paused, reopened.recoveryRequired], [expected, false]);
  const resumed: AgentEvent[] = [];
  const next = await reopened.resume({ result: { region: "eu-west" }, onEvent: (event) => { resumed.push(event); } });
  assert.deepEqual(second.provider.requests[0].history.at(-1),
    { role: "tool_result", callId: "call_ask", toolName: "ask", output: "{\"region\":\"eu-west\"}", isError: false });
  // The run went on and paused again, this time in beforeTool, before the deploy tool ran.
  assert.deepEqual(resumed.map((event) => event.type),
    ["agent_start", "tool_execution_start", "tool_execution_end", "tool_execution_start", "tool_execution_end", "agent_end"]);
  assert.equal(resumed[1].type === "tool_execution_start" && resumed[1].resumed, true);
  assert.deepEqual(next.pause, { toolCallId: "call_deploy", toolName: "deploy", payload: { approve: "deploy" } });
  assert.equal(deploys, 0);
  await reopened.close();

  const third = build(["Deployment was refused."]);
  const last = new Session(third.agent, { ...third.options, sessionId: session.id, resume: true });
  const finished = await last.resume({ error: "Rejected by the release manager" });
  assert.equal(finished.text, "Deployment was refused.");
  assert.deepEqual([third.provider.requests[0].history.at(-1)?.isError, third.provider.requests[0].history.at(-1)?.output],
    [true, "Rejected by the release manager"]);
  assert.equal(deploys, 0);
  assert.deepEqual([last.paused, last.resumable, last.history.length], [undefined, false, 6]);
  await assert.rejects(last.resume({ result: 1 }), { code: "nothing_to_resume" });
  await last.close();
});

test("D28 pause over HTTP and RPC: the page and state show it, resume takes result or error", async () => {
  const agent = () => new Agent({ model: "scripted", tools: [tool("ask", () => pause("why?"))],
    provider: new ScriptedProvider([call("ask"), "thanks"]) });
  const fixture = new HttpFixture(agent());
  const id = await fixture.session();
  const lines = async (text: Promise<string>) => (await text).trimEnd().split("\n").map((line) => object(JSON.parse(line)));
  const paused = await lines(body(await fixture.prompt(id, "go")));
  const pending = { toolCallId: "call_ask", toolName: "ask", payload: "why?" };
  assert.deepEqual([paused.at(-1)?.status, paused.at(-1)?.pause], ["paused", pending]);
  assert.deepEqual((await json(await fixture.send("GET", `/sessions/${id}`))).paused, pending);
  const refused = await fixture.prompt(id, "new");
  assert.equal(object((await json(refused)).error).code, "resume_required");
  const missing = await fixture.send("POST", `/sessions/${id}/resume`);
  assert.equal(missing.statusCode, 400);
  await body(missing);
  const resumed = await lines(body(await fixture.send("POST", `/sessions/${id}/resume`, JSON.stringify({ result: "because" }),
    { "Content-Type": "application/json" })));
  assert.deepEqual(resumed.map((event) => event.type), ["agent_start", "tool_execution_start", "tool_execution_end", "text_delta", "agent_end"]);
  assert.equal(resumed[2].output, "because");
  assert.equal((await json(await fixture.send("GET", `/sessions/${id}`))).paused, null);
  await fixture.dispose();

  const emitted: Item[] = [];
  const rpc = new RpcSession(agent(), (value) => { emitted.push(value as Item); }, () => {});
  await rpc.handle({ id: "p", type: "prompt", message: "go" });
  await rpc.waitIdle();
  assert.equal(emitted.at(-1)?.status, "paused");
  await rpc.handle({ id: "s", type: "get_state" });
  assert.deepEqual(object(emitted.at(-1)?.data).paused, pending);
  await rpc.handle({ id: "r", type: "resume_run", error: "no answer" });
  await rpc.waitIdle();
  assert.deepEqual(emitted.filter((item) => item.type === "tool_execution_end").map((item) => [item.isError, item.output]), [[false, ""], [true, "no answer"]]);
  assert.equal(emitted.at(-1)?.status, "completed");
  await rpc.close();
});

test("D29 outputSchema: valid output, invalid then corrected, a text answer nudged once, a second miss fails", async () => {
  const outputSchema = { type: "object", properties: { city: { type: "string" }, celsius: { type: "number" } },
    required: ["city", "celsius"], additionalProperties: false };
  const valid = { city: "Lisbon", celsius: 24 };
  const run = async (turns: ScriptedTurn[]) => {
    const provider = new ScriptedProvider(turns);
    const session = new Session(new Agent({ model: "scripted", provider }));
    const events: AgentEvent[] = [];
    const outcome: RunResult | Error = await session.run("weather?", { outputSchema, messageId: "m", onEvent: (event) => { events.push(event); } })
      .catch((error: Error) => error);
    return { provider, session, events, outcome };
  };
  const direct = await run([call("final_output", valid)]);
  assert.ok(!(direct.outcome instanceof Error));
  assert.deepEqual(direct.outcome.value, valid);
  const end = direct.events.at(-1);
  assert.ok(end?.type === "agent_end" && end.status === "completed");
  assert.deepEqual(end.value, valid);
  // The synthetic tool is offered next to the agent's own, with the schema as its parameters.
  assert.deepEqual(direct.provider.requests[0].tools.map((offered) => [offered.name, offered.parameters]), [["final_output", outputSchema]]);
  assert.deepEqual(direct.session.history.map((item) => item.role), ["user", "assistant", "tool_result"]);
  assert.deepEqual((await direct.session.run("weather?", { outputSchema, messageId: "m" })).value, valid, "a repeated prompt returns it too");

  const corrected = await run([call("final_output", { city: "Lisbon", celsius: "warm" }), call("final_output", valid, "second")]);
  assert.ok(!(corrected.outcome instanceof Error));
  assert.deepEqual(corrected.outcome.value, valid);
  assert.equal(corrected.provider.requests[1].history.at(-1)?.isError, true);
  assert.match(String(corrected.provider.requests[1].history.at(-1)?.output), /Invalid arguments for final_output\/celsius/);

  const nudged = await run(["It is 24 degrees in Lisbon.", call("final_output", valid)]);
  assert.ok(!(nudged.outcome instanceof Error));
  assert.deepEqual(nudged.outcome.value, valid);
  assert.deepEqual(nudged.provider.requests[1].history.at(-1), { role: "user", content: "Call the final_output tool with your final answer." });

  const missed = await run(["It is warm.", "Still warm."]);
  assert.ok(missed.outcome instanceof RunError && missed.outcome.code === "output_missing");
  assert.equal(missed.provider.requests.length, 2);
  assert.equal(JSON.parse(JSON.stringify(missed.events.at(-1))).error.code, "output_missing");
});

test("D30 the run context reaches handlers and hooks and nothing else", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "ta-e-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const context = { tenant: "SECRET-TENANT-7391", database: { query: () => "row" } };
  const seen: unknown[] = [];
  const provider = new ScriptedProvider([call("lookup"), "done"]);
  const agent = new Agent({ model: "scripted", provider,
    tools: [tool("lookup", (_arguments, _signal, toolContext) => { seen.push(toolContext.context); return "found"; },
      { beforeTool: (_call, toolContext) => { seen.push(toolContext.context); return undefined; } })],
    beforeTool: (_call, toolContext) => { seen.push(toolContext.context); return undefined; },
    afterTool: (_call, _result, toolContext) => { seen.push(toolContext.context); return undefined; } });
  const session = new Session(agent, { directory });
  const events: AgentEvent[] = [];
  await session.run("go", { context, onEvent: (event) => { events.push(event); } });
  assert.deepEqual(seen, [context, context, context, context]);
  assert.equal(seen[0], context, "the very object, not a copy");
  await session.close();
  const everything = readFileSync(join(directory, `${session.id}.jsonl`), "utf8") + JSON.stringify(events) +
    JSON.stringify(provider.requests.map(({ signal: _signal, tools: _tools, ...request }) => request));
  assert.equal(everything.includes("SECRET-TENANT"), false);
  assert.equal(Object.keys(provider.requests[0]).includes("context"), false);
});

test("D34 ScriptedProvider drives a multi-step run and reports the requests it saw", async () => {
  const provider = new ScriptedProvider([
    { text: "Let me look.", toolCalls: [{ callId: "c1", name: "lookup", arguments: { text: "a" } }], usage: { inputTokens: 10, outputTokens: 3 } },
    (request) => call("lookup", { text: `after ${request.history.length} items` }, "c2"),
    { text: "Both found.", deltas: ["Both ", "found."], usage: { inputTokens: 20, outputTokens: 4 } },
  ]);
  assert.equal(provider.remaining, 3);
  const events: AgentEvent[] = [];
  const result = await new Agent({ model: "any-model", instructions: "Be brief.", provider,
    tools: [tool("lookup", ({ text }) => `found ${String(text)}`)] }).run("find both", [], { onEvent: (event) => { events.push(event); } });
  assert.equal(result.text, "Both found.");
  assert.deepEqual(result.usage, { inputTokens: 30, outputTokens: 7, requests: 3 });
  assert.equal(provider.remaining, 0);
  assert.deepEqual(provider.requests.map((request) => [request.model, request.instructions, request.history.length]),
    [["any-model", "Be brief.", 1], ["any-model", "Be brief.", 3], ["any-model", "Be brief.", 5]]);
  assert.equal(provider.requests[2].history.at(-1)?.output, "found after 3 items");
  assert.deepEqual(provider.requests[0].tools.map((offered) => offered.name), ["lookup"]);
  assert.deepEqual(events.filter((event) => event.type === "text_delta").map((event) => event.type === "text_delta" && event.delta),
    ["Let me look.", "Both ", "found."]);
  // The assistant messages it produces are the protocol's: text and tool_call blocks.
  assert.deepEqual(result.history[1], { role: "assistant", content: [{ type: "text", text: "Let me look." },
    { type: "tool_call", callId: "c1", name: "lookup", arguments: { text: "a" } }] });
  await assert.rejects(new Agent({ model: "m", provider }).run("one more"), /no turn for request 4/);
  await assert.rejects(new Agent({ model: "m", provider: new ScriptedProvider([{ error: new Error("scripted failure") }]) }).run("x"),
    /scripted failure/);
});

test("D35 a tool timeout fires the handler's signal and becomes a timed-out tool error", async () => {
  let fired = false;
  const slow = tool("slow", (_arguments, signal) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => { fired = true; reject(new Error("stopped")); });
  }), { timeoutSeconds: 0.05 });
  // This one ignores its signal. The run does not wait for it beyond the grace period.
  const deaf = tool("deaf", () => new Promise(() => {}), { timeoutSeconds: 0.05 });
  const quick = tool("quick", () => "in time", { timeoutSeconds: 5 });
  const provider = new ScriptedProvider([call("slow"), call("quick"), call("deaf"), "moved on"]);
  const started = Date.now();
  const result = await new Agent({ model: "scripted", provider, tools: [slow, quick, deaf] }).run("go");
  assert.equal(result.text, "moved on");
  assert.equal(fired, true);
  assert.deepEqual(provider.requests.slice(1).map((request) => [request.history.at(-1)?.isError, request.history.at(-1)?.output]),
    [[true, "timed out after 0.05 s"], [false, "in time"], [true, "timed out after 0.05 s"]]);
  assert.ok(Date.now() - started < 3000);
  assert.throws(() => tool("bad", () => "", { timeoutSeconds: 0 }), /timeoutSeconds/);
  // Cancelling the run is still cancellation, not a timeout.
  const controller = new AbortController();
  const waiting = tool("slow", (_arguments, signal) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(new Error("stopped")));
    controller.abort();
  }), { timeoutSeconds: 5 });
  await assert.rejects(new Agent({ model: "scripted", provider: new ScriptedProvider([call("slow")]), tools: [waiting] })
    .run("go", [], { signal: controller.signal }), { name: "AbortError" });
  void new MemoryStorage();
});

test("D26 Chat Completions: text, a tool call in fragments, missing usage, errors before and during the stream", async () => {
  const chunk = (delta: Item, finish: string | null = null, extra: Item = {}) =>
    ({ id: "x", object: "chat.completion.chunk", choices: [{ index: 0, delta, finish_reason: finish }], ...extra });
  const bodies: Item[] = [];
  const urls: string[] = [];
  const provider = (...responses: (string | Response)[]) => new ChatCompletionsProvider({ apiKey: "offline",
    baseUrl: "http://models.internal/v1", extra: { temperature: 0 },
    fetch: async (input, init) => {
      urls.push(String(input));
      bodies.push(JSON.parse(String(init?.body)) as Item);
      const next = responses.shift();
      assert.ok(next !== undefined, "unexpected extra request");
      return typeof next === "string" ? new Response(next) : next;
    } });
  const done = "data: [DONE]\n\n";
  // Fragments: the ID and name first, then the arguments as pieces of one JSON string.
  const toolTurn = sse(chunk({ role: "assistant", content: null }),
    chunk({ tool_calls: [{ index: 0, id: "call_9", type: "function", function: { name: "lookup", arguments: "" } }] }),
    chunk({ tool_calls: [{ index: 0, function: { arguments: "{\"te" } }] }),
    chunk({ tool_calls: [{ index: 0, function: { arguments: "xt\":\"Lis" } }] }),
    chunk({ tool_calls: [{ index: 0, function: { arguments: "bon\"}" } }] }),
    chunk({}, "tool_calls"), { choices: [], usage: { prompt_tokens: 12, completion_tokens: 5 } }) + done;
  // No usage at all in the second response.
  const textTurn = sse(chunk({ role: "assistant", content: "" }), chunk({ content: "Clear, " }), chunk({ content: "24 degrees." }), chunk({}, "stop")) + done;
  const events: AgentEvent[] = [];
  const agent = new Agent({ model: "local-model", instructions: "Be brief.", provider: provider(toolTurn, textTurn),
    tools: [tool("lookup", ({ text }) => `forecast for ${String(text)}`)], sleep: async () => {} });
  const result = await agent.run("Lisbon?", [], { onEvent: (event) => { events.push(event); } });
  assert.equal(result.text, "Clear, 24 degrees.");
  assert.deepEqual(result.usage, { inputTokens: 12, outputTokens: 5, requests: 2 });
  assert.deepEqual(events.map((event) => event.type),
    ["agent_start", "tool_execution_start", "tool_execution_end", "text_delta", "text_delta", "agent_end"]);
  assert.deepEqual(result.history[1], { role: "assistant", content: [{ type: "tool_call", callId: "call_9", name: "lookup", arguments: { text: "Lisbon" } }] });
  assert.equal(urls[0], "http://models.internal/v1/chat/completions");
  assert.deepEqual([bodies[0].model, bodies[0].stream, bodies[0].temperature], ["local-model", true, 0]);
  assert.deepEqual(bodies[0].messages, [{ role: "system", content: "Be brief." }, { role: "user", content: "Lisbon?" }]);
  assert.deepEqual((bodies[0].tools as Item[])[0], { type: "function", function: { name: "lookup", description: "lookup",
    parameters: { type: "object", properties: { text: { type: "string" } } } } });
  // The second request replays the call and its result in the API's own shapes.
  assert.deepEqual((bodies[1].messages as Item[]).slice(2), [
    { role: "assistant", content: null, tool_calls: [{ id: "call_9", type: "function", function: { name: "lookup", arguments: "{\"text\":\"Lisbon\"}" } }] },
    { role: "tool", tool_call_id: "call_9", content: "forecast for Lisbon" }]);

  const fails = (response: string | Response) => new Agent({ model: "m", provider: provider(response), sleep: async () => {} }).run("go");
  await assert.rejects(fails(new Response("{\"error\":{\"message\":\"no such model\"}}", { status: 404 })), /API error 404: .*no such model/);
  await assert.rejects(fails(sse(chunk({ content: "par" }), { error: { message: "backend overloaded" } })), /backend overloaded/);
  await assert.rejects(fails(sse(chunk({ content: "partial" }))), /ended before a finish_reason/);
  await assert.rejects(fails(sse(chunk({ content: "long" }), chunk({}, "length")) + done), /incomplete: length/);
  await assert.rejects(fails(sse(chunk({ tool_calls: [{ index: 0, id: "c", function: { name: "lookup", arguments: "{\"te" } }] }), chunk({}, "tool_calls")) + done),
    /invalid arguments for tool lookup/);
  // An error before the stream that is worth retrying is retried by the same loop as the other providers.
  const retried = new Agent({ model: "m", provider: provider(new Response("busy", { status: 503 }), textTurn), sleep: async () => {} });
  assert.equal((await retried.run("go")).text, "Clear, 24 degrees.");
});
