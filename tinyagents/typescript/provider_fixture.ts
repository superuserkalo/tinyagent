import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import type { Item } from "./tiny_agent.ts";

export function fixture(anthropic: boolean, tool = false): Uint8Array {
  const name = (anthropic ? "anthropic" : "openai") + (tool ? "_tool.sse" : "_text.sse");
  return readFileSync(new URL("../../conformance/providers/" + name, import.meta.url));
}

export function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T | PromiseLike<T>) => void;
  reject: (error: unknown) => void;
} {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((accept, fail) => {
    resolve = accept;
    reject = fail;
  });
  return { promise, resolve, reject };
}

export class FixtureBody {
  readonly stream: ReadableStream<Uint8Array>;
  readonly waiting = deferred<void>();
  readonly stopped = deferred<void>();
  cancelled = false;
  produced = 0;

  constructor(
    bytes: Uint8Array,
    options: { chunkSize?: number; hold?: boolean; cleanup?: Promise<void>; failure?: Error } = {},
  ) {
    let offset = 0;
    this.stream = new ReadableStream<Uint8Array>(
      {
        pull: (controller) => {
          if (offset < bytes.length) {
            const end = Math.min(offset + (options.chunkSize ?? 1), bytes.length);
            controller.enqueue(bytes.subarray(offset, end));
            this.produced += end - offset;
            offset = end;
          } else if (options.failure) {
            controller.error(options.failure);
          } else if (!options.hold) {
            controller.close();
          } else {
            this.waiting.resolve();
          }
        },
        cancel: async () => {
          this.cancelled = true;
          this.stopped.resolve();
          await options.cleanup;
        },
      },
      { highWaterMark: 0 },
    );
  }
}

export class FixtureFetch {
  readonly requests: Request[] = [];
  readonly started = deferred<void>();
  readonly bodies: FixtureBody[];
  readonly fetch: typeof globalThis.fetch;

  constructor(bodies: FixtureBody[], options: { holdHeaders?: boolean; status?: number } = {}) {
    this.bodies = bodies;
    this.fetch = async (input, init) => {
      const request = new Request(input, init);
      this.requests.push(request);
      this.started.resolve();
      if (options.holdHeaders) {
        return await new Promise<Response>((_resolve, reject) => {
          if (request.signal.aborted) reject(request.signal.reason);
          else request.signal.addEventListener("abort", () => reject(request.signal.reason), { once: true });
        });
      }
      const body = bodies[this.requests.length - 1];
      assert.ok(body, "fixture received an extra request");
      return new Response(body.stream, {
        status: options.status ?? 200,
        headers: { "Content-Type": "text/event-stream" },
      });
    };
  }

  async payload(index: number): Promise<Item> {
    const value: unknown = JSON.parse(await this.requests[index].text());
    assert.ok(typeof value === "object" && value !== null && !Array.isArray(value));
    return value as Item;
  }
}
