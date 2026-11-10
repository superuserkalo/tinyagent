import assert from "node:assert/strict";
import { Agent as HttpAgent, request, type IncomingMessage, type OutgoingHttpHeaders } from "node:http";
import { Duplex } from "node:stream";
import { setTimeout as delay } from "node:timers/promises";
import type { Agent, Item, ServerOptions } from "./tiny_agent.ts";
import { createHttpServer } from "./http.ts";

export function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

export async function until(check: () => boolean): Promise<void> {
  const deadline = Date.now() + 3000;
  while (!check()) {
    if (Date.now() >= deadline) throw new Error("HTTP fixture did not reach the expected state");
    await delay(5);
  }
}

export function object(value: unknown): Item {
  assert.ok(typeof value === "object" && value !== null && !Array.isArray(value));
  return value as Item;
}

export async function body(response: IncomingMessage): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of response as AsyncIterable<unknown>) {
    assert.ok(Buffer.isBuffer(chunk));
    chunks.push(chunk);
  }
  assert.equal(response.complete, true, "the HTTP message must include its complete framing");
  return Buffer.concat(chunks).toString("utf8");
}

export async function json(response: IncomingMessage): Promise<Item> {
  return object(JSON.parse(await body(response)));
}

/** A write acknowledges delivery into the peer's bounded readable buffer. */
class MemorySocket extends Duplex {
  peer!: MemorySocket;
  private pendingRead: ((error?: Error | null) => void) | undefined;
  private readonly beforeWrite: ((chunk: Buffer) => void | Promise<void>) | undefined;

  constructor(beforeWrite?: (chunk: Buffer) => void | Promise<void>) {
    super({ highWaterMark: 1024, allowHalfOpen: false });
    this.beforeWrite = beforeWrite;
  }

  override _read(): void {
    const pending = this.pendingRead;
    this.pendingRead = undefined;
    pending?.();
  }

  override _write(chunk: Buffer, _encoding: BufferEncoding, callback: (error?: Error | null) => void): void {
    Promise.resolve()
      .then(() => this.beforeWrite?.(chunk))
      .then(
        () => {
          if (this.destroyed || this.peer.destroyed) callback(new Error("Memory connection closed"));
          else if (this.peer.push(Buffer.from(chunk))) callback();
          else this.peer.pendingRead = callback;
        },
        (error: unknown) => callback(error instanceof Error ? error : new Error(String(error))),
      );
  }

  override _final(callback: (error?: Error | null) => void): void {
    if (!this.peer.destroyed) this.peer.push(null);
    callback();
  }

  override _destroy(error: Error | null, callback: (error?: Error | null) => void): void {
    const pending = this.pendingRead;
    this.pendingRead = undefined;
    pending?.(error ?? new Error("Memory connection closed"));
    if (!this.peer.destroyed) this.peer.destroy(error ?? undefined);
    callback(error);
  }
}

export class HttpFixture {
  readonly application: ReturnType<typeof createHttpServer>;
  private readonly sockets: Duplex[] = [];
  private readonly clients: HttpAgent[] = [];

  constructor(agent: Agent, options: ServerOptions = {}) {
    this.application = createHttpServer(agent, options);
  }

  open(
    method: string,
    path: string,
    headers: OutgoingHttpHeaders | readonly string[] = {},
    beforeWrite?: (chunk: Buffer) => void | Promise<void>,
  ) {
    const client = new MemorySocket();
    const server = new MemorySocket(beforeWrite);
    client.peer = server;
    server.peer = client;
    this.sockets.push(client, server);
    const agent = new HttpAgent({ keepAlive: false });
    this.clients.push(agent);
    agent.createConnection = () => {
      this.application.server.emit("connection", server);
      return client;
    };
    const outgoing = request({ host: "fixture.invalid", method, path, headers, agent });
    const response = new Promise<IncomingMessage>((resolve, reject) => {
      outgoing.once("response", (incoming) => {
        incoming.on("error", () => {});
        resolve(incoming);
      });
      outgoing.once("error", reject);
    });
    return { request: outgoing, response, client, server };
  }

  async send(method: string, path: string, data?: string | Buffer, headers: OutgoingHttpHeaders = {}) {
    const exchange = this.open(method, path, headers);
    exchange.request.end(data);
    return await exchange.response;
  }

  async session(headers: OutgoingHttpHeaders = {}): Promise<string> {
    const response = await this.send("POST", "/sessions", undefined, headers);
    assert.equal(response.statusCode, 201);
    const id = (await json(response)).sessionId;
    assert.equal(typeof id, "string");
    return String(id);
  }

  async prompt(id: string, message: string) {
    return await this.send("POST", "/sessions/" + id + "/prompts", JSON.stringify({ message }), {
      "Content-Type": "application/json",
    });
  }

  async dispose(): Promise<void> {
    for (const socket of this.sockets) socket.destroy();
    for (const agent of this.clients) agent.destroy();
    await this.application.close();
  }
}
