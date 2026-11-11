// The arguments object of a tool call is one object shared by the event, the hooks, the handler,
// the transcript and the provider replay data. Whoever mutates it changes what the tool runs with
// or what the next model request contains, while the journal keeps the original.
import test from "node:test";
import assert from "node:assert/strict";
import { Agent, Session, Tool, AnthropicProvider, ScriptedProvider, type Item } from "./tiny_agent.ts";

test("G09 an onEvent consumer that redacts a tool_execution_start does not change what the tool receives", async () => {
  let received: Item | undefined;
  const login = new Tool({ name: "login", description: "login", parameters: { type: "object" },
    handler: (arguments_) => { received = { ...arguments_ }; return "ok"; } });
  const agent = new Agent({ model: "m", tools: [login],
    provider: new ScriptedProvider([[{ callId: "c1", name: "login", arguments: { password: "hunter2" } }], "done"]) });
  await agent.run("go", [], { onEvent: (event) => {
    if (event.type === "tool_execution_start") event.arguments.password = "***";   // redact before logging
  } });
  assert.deepEqual(received, { password: "hunter2" });
});

test("G09 a handler that adds a credential to its arguments does not send it to the model provider", async () => {
  const payloads: string[] = [];
  let request = 0;
  const provider = new AnthropicProvider({ apiKey: "test", async *eventTransport(payload: Item) {
    payloads.push(JSON.stringify(payload));
    request += 1;
    yield { type: "message_start", message: { id: "m", role: "assistant", content: [], usage: { input_tokens: 1, output_tokens: 1 } } };
    if (request === 1) {
      yield { type: "content_block_start", index: 0, content_block: { type: "tool_use", id: "c1", name: "call_api", input: {} } };
      yield { type: "content_block_delta", index: 0, delta: { type: "input_json_delta", partial_json: "{\"path\":\"/users\"}" } };
    } else {
      yield { type: "content_block_start", index: 0, content_block: { type: "text", text: "" } };
      yield { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "done" } };
    }
    yield { type: "content_block_stop", index: 0 };
    yield { type: "message_delta", delta: { stop_reason: request === 1 ? "tool_use" : "end_turn" }, usage: { output_tokens: 1 } };
    yield { type: "message_stop" };
  } });
  const callApi = new Tool({ name: "call_api", description: "call the API", parameters: { type: "object" },
    handler: (arguments_) => { arguments_.authorization = "Bearer SECRET-TOKEN"; return "200 OK"; } });
  const session = new Session(new Agent({ model: "m", provider, tools: [callApi] }));
  await session.run("list users");
  assert.equal(payloads.length, 2);
  assert.ok(!JSON.stringify(session.records).includes("SECRET-TOKEN"), "the journal never held the credential");
  assert.ok(!payloads[1].includes("SECRET-TOKEN"), "the second model request carries the handler's mutation");
});
