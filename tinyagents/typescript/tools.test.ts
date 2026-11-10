import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { Agent, Tool, type AgentEvent, type Item, type Provider } from "./tiny_agent.ts";

interface Fixture {
  cases: { name: string; schema: Item; valid: Item[]; invalid: Item[] }[];
}

const fixture = JSON.parse(
  readFileSync(new URL("../../conformance/tool_arguments.json", import.meta.url), "utf8"),
) as Fixture;

for (const scenario of fixture.cases) {
  test(`tool arguments: ${scenario.name}`, () => {
    let calls = 0;
    const tool = new Tool({
      name: "check",
      description: "Check",
      parameters: scenario.schema,
      handler: () => {
        calls += 1;
        return "ok";
      },
    });
    const signal = new AbortController().signal;
    for (const valid of scenario.valid) assert.equal(tool.execute(valid, signal), "ok");
    const calledBefore = calls;
    for (const invalid of scenario.invalid) {
      const original = structuredClone(invalid);
      assert.throws(() => tool.execute(invalid, signal), /Invalid arguments/);
      assert.deepEqual(invalid, original, "validation must not coerce or remove values");
    }
    assert.equal(calls, calledBefore, "invalid input must never reach the handler");
  });
}

test("invalid schemas and unresolved remote references fail before a run", () => {
  for (const parameters of [{ type: "nonsense" }, { $ref: "https://example.invalid/schema" }, { $async: true }]) {
    assert.throws(() => new Tool({ name: "test", description: "test", parameters, handler: () => "unused" }));
  }
});

test("schema failure becomes a recoverable tool observation", async () => {
  let handled = false;
  let requests = 0;
  const tool = new Tool({
    name: "echo",
    description: "Echo",
    parameters: fixture.cases[0].schema,
    handler: () => {
      handled = true;
      return "unexpected";
    },
  });
  const provider: Provider = {
    async *stream(request) {
      requests += 1;
      if (requests === 1)
        return {
          text: "",
          assistant: { role: "assistant", content: [] },
          output: [],
          toolCalls: [{ callId: "bad", name: "echo", arguments: { text: 42 } }],
        };
      assert.equal(request.history.at(-1)?.isError, true);
      assert.match(String(request.history.at(-1)?.output), /Invalid arguments/);
      return { text: "recovered", assistant: { role: "assistant", content: [] }, output: [], toolCalls: [] };
    },
  };
  const events: AgentEvent[] = [];
  const result = await new Agent({ model: "test", tools: [tool], provider }).run("go", [], {
    onEvent: (event) => {
      events.push(event);
    },
  });
  assert.equal(result.text, "recovered");
  assert.equal(handled, false);
  assert.deepEqual(
    events.map((event) => event.type),
    ["agent_start", "tool_execution_start", "tool_execution_end", "agent_end"],
  );
});
