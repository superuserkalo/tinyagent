// Invariant 2: a model turn with no content blocks (Claude does end a turn that way now and then)
// is committed and replayed as { role: "assistant", content: [] }. The Messages API rejects an
// empty non-final message, so every later prompt of that session fails. This test proves the
// request shape; that the API answers 400 for it is from its documentation, not from this test.
import test from "node:test";
import assert from "node:assert/strict";
import { Agent, Session, AnthropicProvider, type Item } from "./tiny_agent.ts";

test("G03 an empty assistant turn is not replayed to Anthropic as a message without content", async () => {
  const payloads: Item[] = [];
  const provider = new AnthropicProvider({ apiKey: "test", async *eventTransport(payload: Item) {
    payloads.push(structuredClone(payload));
    yield { type: "message_start", message: { id: "m", role: "assistant", content: [], usage: { input_tokens: 1, output_tokens: 0 } } };
    yield { type: "message_delta", delta: { stop_reason: "end_turn" }, usage: { output_tokens: 0 } };
    yield { type: "message_stop" };
  } });
  const session = new Session(new Agent({ model: "m", provider }));
  await session.run("one");
  await session.run("two");
  const messages = payloads[1].messages as Item[];
  for (const message of messages)
    assert.ok(!(Array.isArray(message.content) && message.content.length === 0), `request held ${JSON.stringify(messages)}`);
});
