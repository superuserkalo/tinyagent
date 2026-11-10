import assert from "node:assert/strict";
import test from "node:test";
import {
  Agent,
  AnthropicProvider,
  MAX_TOOL_OUTPUT_BYTES,
  OpenAIProvider,
  ProviderError,
  Tool,
  type AgentEvent,
  type Item,
  type Provider,
  type ProviderTurn,
} from "./tiny_agent.ts";

function turn(text: string): ProviderTurn {
  return { text, assistant: { role: "assistant", content: [{ type: "text", text }] }, output: [], toolCalls: [] };
}

test("cancellation wins when a provider returns after its signal is aborted", async () => {
  const controller = new AbortController();
  const emitted: AgentEvent[] = [];
  const provider: Provider = {
    async *stream() {
      controller.abort();
      return turn("too late");
    },
  };
  await assert.rejects(
    new Agent({ model: "test", provider }).run("go", [], {
      signal: controller.signal,
      onEvent: (event) => {
        emitted.push(event);
      },
    }),
    { name: "AbortError" },
  );
  assert.deepEqual(
    emitted.map((event) => event.type),
    ["agent_start", "agent_end"],
  );
  const terminal = emitted.at(-1);
  assert.equal(terminal?.type === "agent_end" && terminal.status, "aborted");
});

test("early stream exit aborts and closes the provider before cleanup returns", async () => {
  let closed = false;
  const provider: Provider = {
    async *stream(request) {
      try {
        yield { type: "text_delta", delta: "one" };
        return turn("done");
      } finally {
        assert.equal(request.signal.aborted, true);
        closed = true;
      }
    },
  };
  for await (const event of new Agent({ model: "test", provider }).stream("go")) {
    if (event.type === "text_delta") break;
  }
  assert.equal(closed, true);
});

test("a failing event consumer closes the provider", async () => {
  let closed = false;
  const provider: Provider = {
    async *stream(request) {
      try {
        yield { type: "text_delta", delta: "one" };
        return turn("done");
      } finally {
        assert.equal(request.signal.aborted, true);
        closed = true;
      }
    },
  };
  await assert.rejects(
    new Agent({ model: "test", provider }).run("go", [], {
      onEvent(event) {
        if (event.type === "text_delta") throw new Error("writer closed");
      },
    }),
    /writer closed/,
  );
  assert.equal(closed, true);
});

test("failed providers cannot mutate caller history", async () => {
  const history: Item[] = [{ role: "user", content: "keep" }];
  const provider: Provider = {
    async *stream(request) {
      request.history[0].content = "mutated";
      throw new ProviderError("failed");
    },
  };
  await assert.rejects(new Agent({ model: "test", provider }).run("go", history), /failed/);
  assert.deepEqual(history, [{ role: "user", content: "keep" }]);
});

test("oversized tool exceptions become bounded recoverable observations", async () => {
  let calls = 0;
  const tool = new Tool({
    name: "fail",
    description: "Fail",
    parameters: { type: "object" },
    handler() {
      throw new Error("🔥".repeat(MAX_TOOL_OUTPUT_BYTES));
    },
  });
  const provider: Provider = {
    async *stream(request) {
      calls += 1;
      if (calls === 1) return { ...turn(""), toolCalls: [{ callId: "c", name: "fail", arguments: {} }] };
      const observation = request.history.at(-1);
      assert.equal(observation?.isError, true);
      assert.ok(Buffer.byteLength(String(observation?.output)) < 200);
      return turn("recovered");
    },
  };
  const result = await new Agent({ model: "test", provider, tools: [tool] }).run("go");
  assert.equal(result.text, "recovered");
});

test("providers stop at terminal events without waiting for HTTP EOF", async () => {
  let openaiClosed = false;
  const openai = new OpenAIProvider({
    async *eventTransport() {
      try {
        yield { type: "response.completed", response: { output: [] } };
        throw new Error("read past response.completed");
      } finally {
        openaiClosed = true;
      }
    },
  });
  await new Agent({ model: "test", provider: openai }).run("go");
  assert.equal(openaiClosed, true);

  let anthropicClosed = false;
  const anthropic = new AnthropicProvider({
    async *eventTransport() {
      try {
        yield { type: "message_start", message: { id: "m", content: [] } };
        yield { type: "message_delta", delta: { stop_reason: "end_turn" } };
        yield { type: "message_stop" };
        throw new Error("read past message_stop");
      } finally {
        anthropicClosed = true;
      }
    },
  });
  await new Agent({ model: "test", provider: anthropic }).run("go");
  assert.equal(anthropicClosed, true);
});

for (const api of ["run", "stream"]) {
  for (const failure of ["cancel", "consumer"]) {
    test(`${api}: terminal ${failure} prevents a result without a second terminal event`, async () => {
      let closed = 0;
      const provider: Provider = {
        async *stream() {
          try {
            yield { type: "text_delta", delta: "done" };
            return turn("done");
          } finally {
            closed++;
          }
        },
      };
      const agent = new Agent({ model: "test", provider });
      const controller = new AbortController();
      const history = [{ role: "user", content: "earlier" }];
      const events: AgentEvent[] = [];
      const consumerError = new Error("terminal delivery failed");
      await assert.rejects(async () => {
        if (api === "run") {
          await agent.run("go", history, {
            signal: controller.signal,
            onEvent(event) {
              events.push(event);
              if (event.type === "agent_end") {
                assert.equal(closed, 1);
                if (failure === "cancel") controller.abort();
                else throw consumerError;
              }
            },
          });
        } else {
          const stream = agent.stream("go", history, { signal: controller.signal });
          try {
            for await (const event of stream) {
              events.push(event);
              if (event.type === "agent_end") {
                assert.equal(closed, 1);
                if (failure === "cancel") {
                  controller.abort();
                  await stream.next();
                } else {
                  await stream.throw(consumerError);
                  assert.fail("stream yielded another terminal event");
                }
              }
            }
          } finally {
            await stream.return(undefined as never);
          }
        }
      }, (error: unknown) => failure === "consumer"
        ? error === consumerError
        : error instanceof Error && error.name === "AbortError");
      assert.deepEqual(events.map((event) => event.type), ["agent_start", "text_delta", "agent_end"]);
      assert.deepEqual(events.map((event) => event.sequence), [1, 2, 3]);
      assert.equal(new Set(events.map((event) => event.runId)).size, 1);
      assert.deepEqual(history, [{ role: "user", content: "earlier" }]);
      assert.equal((await agent.run("again", history)).text, "done");
      assert.equal(closed, 2);
    });
  }
}
