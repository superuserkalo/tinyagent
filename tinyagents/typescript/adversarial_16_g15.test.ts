// Invariant 2: history sent to a provider is valid. In Chat Completions every tool message must
// directly follow the assistant message's tool_calls. The provider puts a tool result's images
// in a user message right after that tool message, which lands between two tool messages when
// the turn made more than one call.
import test from "node:test";
import assert from "node:assert/strict";
import { ChatCompletionsProvider, type Item } from "./tiny_agent.ts";

test("G15 images of a tool result do not split the tool messages of one assistant turn", async () => {
  const payloads: Item[] = [];
  const provider = new ChatCompletionsProvider({ async *eventTransport(payload: Item) {
    payloads.push(payload);
    yield { choices: [{ delta: { content: "ok" }, finish_reason: "stop" }] };
    yield { done: true };
  } });
  const history = [
    { role: "user", content: "compare the two pages" },
    { role: "assistant", content: [{ type: "tool_call", callId: "a", name: "shot", arguments: {} }, { type: "tool_call", callId: "b", name: "shot", arguments: {} }] },
    { role: "tool_result", callId: "a", toolName: "shot", isError: false, output: [{ type: "text", text: "page one" }, { type: "image", url: "https://example.invalid/1.png" }] },
    { role: "tool_result", callId: "b", toolName: "shot", isError: false, output: "page two" },
  ];
  const stream = provider.stream({ model: "m", instructions: "", history, tools: [], signal: new AbortController().signal });
  while (!(await stream.next()).done);
  const roles = (payloads[0].messages as Item[]).map((message) => String(message.role));
  const assistant = roles.indexOf("assistant");
  assert.deepEqual(roles.slice(assistant + 1, assistant + 3), ["tool", "tool"], `message order was ${roles.join(", ")}`);
});
