import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import { Agent, AnthropicProvider, OpenAIProvider } from "./tiny_agent.ts";
import { deferred, fixture } from "./provider_fixture.ts";

for (const anthropic of [false, true]) {
  test((anthropic ? "Anthropic" : "OpenAI") + " real TCP completion and cancellation", { timeout: 5000 }, async () => {
    const completedClosed = deferred<void>();
    const blockedStarted = deferred<void>();
    const blockedClosed = deferred<void>();
    const server = createServer((request, response) => {
      request.resume();
      const complete = request.url === "/complete";
      response.writeHead(200, { "Content-Type": "text/event-stream" });
      response.on("close", () => (complete ? completedClosed : blockedClosed).resolve());
      response.write(complete ? fixture(anthropic) : Buffer.from(": waiting\n\n"));
      if (!complete) blockedStarted.resolve();
      // An unfinished chunked body proves that terminal parsing/cancellation
      // releases the connection without waiting for the server's EOF.
    });
    // Binding is mandatory. A restricted environment must report failure.
    await new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
    try {
      const address = server.address();
      assert.ok(address && typeof address !== "string");
      const base = "http://127.0.0.1:" + address.port;
      const options = { apiKey: "fixture-key", timeoutMs: 2000 };
      const complete = anthropic
        ? new AnthropicProvider({ ...options, endpoint: base + "/complete" })
        : new OpenAIProvider({ ...options, endpoint: base + "/complete" });
      assert.equal((await new Agent({ model: "fixture", provider: complete }).run("go")).text, "done\ncafé");
      await completedClosed.promise;
      const blocked = anthropic
        ? new AnthropicProvider({ ...options, endpoint: base + "/blocked" })
        : new OpenAIProvider({ ...options, endpoint: base + "/blocked" });
      const controller = new AbortController();
      const run = new Agent({ model: "fixture", provider: blocked }).run("go", [], { signal: controller.signal });
      const checked = assert.rejects(run, { name: "AbortError" });
      try {
        await blockedStarted.promise;
        controller.abort();
        await checked;
        await blockedClosed.promise;
      } finally {
        controller.abort();
        await checked;
      }
    } finally {
      server.closeAllConnections();
      await new Promise<void>((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
    }
  });
}
