import { randomUUID, timingSafeEqual } from "node:crypto";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { isIP } from "node:net";
import { Session, RecoveryRequiredError, type ContextBudget } from "./session.ts";
import { SessionBusyError, SessionCorruptionError } from "./storage.ts";
import { isItem } from "./types.ts";
import type { Agent, AgentEvent, Item } from "./tiny_agent.ts";
import { MAX_HTTP_BODY_BYTES, MAX_JSONL_LINE_BYTES, PROTOCOL_VERSION } from "./protocol.ts";

export interface ServerOptions {
  host?: string;
  port?: number;
  token?: string;
  allowRemote?: boolean;
  directory?: string;
  budget?: ContextBudget;
}

export interface AgentServer {
  readonly url: string;
  /** Stop accepting requests, cancel runs, and wait for cooperative cleanup. */
  close(): Promise<void>;
}

interface HttpApplication {
  readonly server: Server;
  readonly host: string;
  readonly port: number;
  close(): Promise<void>;
}

interface ActiveRun {
  runId: string;
  controller: AbortController;
  done: Promise<void>;
}

interface HttpSession {
  core: Session;
  active?: ActiveRun;
  deleted: boolean;
}

class HttpError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

/** Construct the native server separately from binding its listener. */
export function createHttpServer(agent: Agent, options: ServerOptions = {}): HttpApplication {
  const host = options.host === "localhost" ? "127.0.0.1" : (options.host ?? "127.0.0.1");
  if (!isIP(host)) throw new Error("host must be an IP address or localhost");
  const loopback = host === "::1" || (isIP(host) === 4 && host.startsWith("127."));
  const token = options.token;
  if (token !== undefined && !/^[\x21-\x7e]+$/.test(token))
    throw new Error("token must contain non-space ASCII characters");
  if (!loopback && (!options.allowRemote || !token))
    throw new Error("remote binding requires allowRemote and a bearer token");
  const port = options.port ?? 8080;
  if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error("port must be an integer from 0 to 65535");

  const sessions = new Map<string, HttpSession>();
  let closing = false;
  const server = createServer({ requestTimeout: 30_000 }, (request, response) => {
    void route(request, response).catch(async (error: unknown) => {
      if (response.destroyed) return;
      if (response.headersSent) {
        response.destroy();
        return;
      }
      await requestReceived(request);
      if (response.destroyed) return;
      const failure = error instanceof HttpError ? error
        : error instanceof SessionBusyError ? new HttpError(409, "session_busy", error.message)
        : error instanceof RecoveryRequiredError ? new HttpError(409, "recovery_required", error.message)
        : error instanceof SessionCorruptionError ? new HttpError(409, "session_corrupt", error.message)
        : error instanceof TypeError ? new HttpError(400, "invalid_request", error.message)
        : isItem(error) && error.code === "ENOENT" ? new HttpError(404, "session_not_found", "Unknown session")
        : new HttpError(500, "internal_error", "Request failed");
      response.setHeader("Connection", "close");
      sendJson(response, failure.status, {
        error: { code: failure.code, message: failure.message, retryable: failure.status >= 500 },
      });
    });
  });

  async function route(request: IncomingMessage, response: ServerResponse): Promise<void> {
    if (token) {
      const expected = Buffer.from(`Bearer ${token}`);
      const credentials = request.headersDistinct.authorization;
      const actual = Buffer.from(credentials?.[0] ?? "");
      if (credentials?.length !== 1 || actual.length !== expected.length || !timingSafeEqual(actual, expected))
        throw new HttpError(401, "unauthorized", "A valid bearer token is required");
    }
    if (closing) throw new HttpError(503, "server_closing", "Server is shutting down");
    const path = (request.url ?? "").split("?")[0];
    if (request.method === "GET" && path === "/health") {
      sendJson(response, 200, { status: "ok", protocolVersion: PROTOCOL_VERSION });
      return;
    }
    if (request.method === "GET" && path === "/sessions") {
      sendJson(response, 200, { sessionIds: [...new Set([...sessions.keys(),
        ...(options.directory ? Session.list(options.directory) : [])])].sort() });
      return;
    }
    if (request.method === "POST" && path === "/sessions") {
      const body = await readBody(request, false);
      if (closing) throw new HttpError(503, "server_closing", "Server is shutting down");
      const identifier = body?.sessionId;
      if (identifier !== undefined && (typeof identifier !== "string" || !options.directory))
        throw new HttpError(400, "invalid_request", "resume requires storage and a string sessionId");
      if (typeof identifier === "string" && sessions.has(identifier))
        throw new HttpError(409, "session_busy", "Session is already open");
      const core = typeof identifier === "string" && options.directory
        ? Session.open(agent, options.directory, identifier, options.budget)
        : new Session(agent, { directory: options.directory, budget: options.budget });
      sessions.set(core.id, { core, deleted: false });
      sendJson(response, 201, { sessionId: core.id });
      return;
    }
    const match = /^\/sessions\/([^/]+)(?:\/(prompts|cancel|compact|reconcile))?$/.exec(path);
    if (!match) throw new HttpError(404, "not_found", "Unknown route");
    const sessionId = match[1];
    const session = sessions.get(sessionId);
    if (!session || session.deleted) throw new HttpError(404, "session_not_found", "Unknown session");

    if (request.method === "GET" && !match[2]) {
      const query = new URL(request.url ?? "", "http://localhost").searchParams;
      if ([...query.keys()].some((key) => query.getAll(key).length !== 1))
        throw new HttpError(400, "invalid_request", "Duplicate inspection options");
      sendJson(response, 200, session.core.inspect({ offset: Number(query.get("offset") ?? 0),
        limit: Number(query.get("limit") ?? 20), view: query.get("view") ?? "history" }));
    } else if (request.method === "POST" && match[2] === "reconcile") {
      const body = await readBody(request, true);
      if (session.active) throw new HttpError(409, "session_busy", "Session has an active run");
      if (session.deleted || closing) throw new HttpError(409, "session_closed", "Session is closing");
      if (!body || typeof body.note !== "string" || !body.note.trim())
        throw new HttpError(400, "invalid_request", "reconciliation requires a note");
      session.core.reconcile(body.note);
      sendJson(response, 200, { recoveryRequired: false });
    } else if (request.method === "POST" && (match[2] === "prompts" || match[2] === "compact")) {
      const compact = match[2] === "compact";
      const body = await readBody(request, !compact);
      if (!compact && (!body || typeof body.message !== "string" || !body.message))
        throw new HttpError(400, "invalid_request", "message must be a non-empty string");
      if (session.deleted) throw new HttpError(404, "session_not_found", "Unknown session");
      if (closing) throw new HttpError(503, "server_closing", "Server is shutting down");
      if (session.core.recoveryRequired)
        throw new HttpError(409, "recovery_required", "Inspect history and reconcile interrupted tool effects");
      if (compact && !options.budget) throw new HttpError(400, "budget_required", "Configure a compaction budget");
      if (session.active) {
        sendJson(response, 409, {
          error: { code: "session_busy", message: "The session already has an active run", retryable: true },
        });
        return;
      }
      const active: ActiveRun = {
        runId: `r_${randomUUID().replaceAll("-", "")}`,
        controller: new AbortController(),
        done: Promise.resolve(),
      };
      session.active = active;
      response.setHeader("Content-Type", "application/jsonl; charset=utf-8");
      response.setHeader("Cache-Control", "no-store");
      response.setHeader("X-Content-Type-Options", "nosniff");
      active.done = execute(session, active, typeof body?.message === "string" ? body.message : "", response, compact);
      await active.done;
    } else if (request.method === "POST" && match[2] === "cancel") {
      await readBody(request, false);
      if (session.deleted) throw new HttpError(404, "session_not_found", "Unknown session");
      if (closing) throw new HttpError(503, "server_closing", "Server is shutting down");
      const active = session.active;
      active?.controller.abort();
      sendJson(response, 200, { accepted: Boolean(active), runId: active?.runId ?? null });
    } else if (request.method === "DELETE" && !match[2]) {
      session.deleted = true;
      session.active?.controller.abort();
      await session.active?.done;
      await session.core.close();
      sessions.delete(sessionId);
      response.writeHead(204).end();
    } else {
      throw new HttpError(404, "not_found", "Unknown route");
    }
  }

  async function execute(session: HttpSession, active: ActiveRun, prompt: string, response: ServerResponse,
    compact: boolean): Promise<void> {
    const disconnect = () => {
      if (!response.writableFinished) active.controller.abort();
    };
    response.on("close", disconnect);
    response.on("error", disconnect);
    try {
      const runOptions = {
        runId: active.runId,
        signal: active.controller.signal,
        onEvent: async (event: AgentEvent) => {
          let line = `${JSON.stringify(event)}\n`;
          if (Buffer.byteLength(line) - 1 > MAX_JSONL_LINE_BYTES) {
            line = `${JSON.stringify({
              type: "agent_end",
              runId: event.runId,
              sequence: event.sequence,
              status: "failed",
              error: { code: "output_too_large", message: "Event exceeds the JSONL byte limit", retryable: false },
            })}\n`;
            active.controller.abort();
            await writeLine(response, line, active.controller.signal, true);
            throw new Error("Event exceeds the JSONL byte limit");
          }
          await writeLine(response, line, active.controller.signal,
            event.type === "agent_end" || (compact && event.type === "compaction_end"));
        },
      };
      if (compact) await session.core.compact(runOptions);
      else await session.core.run(prompt, runOptions);
    } catch {
      // Agent.run emits failures. A disconnected client cannot receive another event.
      active.controller.abort();
    } finally {
      session.active = undefined;
      if (!response.writableFinished && !response.destroyed) response.destroy();
      response.off("close", disconnect);
      response.off("error", disconnect);
    }
  }

  let closePromise: Promise<void> | undefined;
  return {
    server,
    host,
    port,
    close() {
      if (!closePromise) {
        closing = true;
        closePromise = (async () => {
          const stopped = server.listening
            ? new Promise<void>((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())))
            : Promise.resolve();
          const runs: Promise<void>[] = [];
          for (const session of sessions.values()) {
            session.deleted = true;
            if (session.active) {
              session.active.controller.abort();
              runs.push(session.active.done);
            }
          }
          await Promise.allSettled(runs);
          for (const session of sessions.values()) await session.core.close();
          server.closeAllConnections();
          await stopped;
          sessions.clear();
        })();
      }
      return closePromise;
    },
  };
}

/** Serve one configured Agent. All model and tool execution stays in Agent.run. */
export async function serve(agent: Agent, options: ServerOptions = {}): Promise<AgentServer> {
  const application = createHttpServer(agent, options);
  const { server, host, port } = application;
  try {
    await new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen(port, host, () => {
        server.off("error", reject);
        resolve();
      });
    });
    const address = server.address();
    if (!address || typeof address === "string") throw new Error("Server has no TCP address");
    return {
      url: `http://${isIP(host) === 6 ? `[${host}]` : host}:${address.port}`,
      close: application.close,
    };
  } catch (error) {
    await application.close();
    throw error;
  }
}

/**
 * Closing a connection whose client is still sending resets it, and the client
 * may never read the error. Let the rest of the request arrive first, for at
 * most a second.
 */
function requestReceived(request: IncomingMessage): Promise<void> {
  return new Promise((resolve) => {
    if (request.complete || request.destroyed) {
      resolve();
      return;
    }
    const done = () => {
      clearTimeout(timer);
      request.off("end", done);
      request.off("close", done);
      resolve();
    };
    const timer = setTimeout(done, 1000);
    request.once("end", done);
    request.once("close", done);
    request.resume();
  });
}

async function readBody(request: IncomingMessage, required: boolean): Promise<Item | undefined> {
  const contentType = request.headers["content-type"]?.split(";")[0].trim().toLowerCase();
  if (required && contentType !== "application/json")
    throw new HttpError(415, "unsupported_media_type", "Content-Type must be application/json");
  if (Number(request.headers["content-length"]) > MAX_HTTP_BODY_BYTES)
    throw new HttpError(413, "request_too_large", "Request body exceeds the byte limit");
  const chunks: Buffer[] = [];
  let bytes = 0;
  for await (const chunk of request.iterator({ destroyOnReturn: false }) as AsyncIterable<unknown>) {
    const data = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk));
    bytes += data.length;
    if (bytes > MAX_HTTP_BODY_BYTES)
      throw new HttpError(413, "request_too_large", "Request body exceeds the byte limit");
    chunks.push(data);
  }
  if (!bytes && !required) return undefined;
  let body: unknown;
  try {
    body = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)));
  } catch {
    throw new HttpError(400, "invalid_json", "Request body must be valid UTF-8 JSON");
  }
  if (typeof body !== "object" || body === null || Array.isArray(body))
    throw new HttpError(400, "invalid_request", "Request body must be an object");
  return body as Item;
}

function sendJson(response: ServerResponse, status: number, value: object): void {
  response
    .writeHead(status, {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
    })
    .end(JSON.stringify(value));
}

/** Await each write, keeping at most one bounded event in the output buffer. */
function writeLine(response: ServerResponse, line: string, signal: AbortSignal, terminal: boolean): Promise<void> {
  return new Promise((resolve, reject) => {
    if (response.destroyed || response.writableEnded) {
      reject(new Error("Client disconnected"));
      return;
    }
    let timer: ReturnType<typeof setTimeout> | undefined;
    let settled = false;
    const socket = response.socket;
    const cancel = () => {
      timer ??= setTimeout(() => response.destroy(), 1000);
    };
    const closed = () => finish(new Error("Client disconnected"));
    const finish = (error?: Error | null) => {
      if (settled) return;
      settled = true;
      // Node 22 reports a finished response even when its last socket write failed.
      error ??= socket?.errored;
      clearTimeout(timer);
      signal.removeEventListener("abort", cancel);
      response.off("close", closed);
      response.off("error", finish);
      if (error) reject(error);
      else resolve();
    };
    response.once("close", closed);
    response.once("error", finish);
    signal.addEventListener("abort", cancel, { once: true });
    if (signal.aborted) cancel();
    try {
      if (terminal) response.end(line, finish);
      else response.write(line, finish);
    } catch (error) {
      finish(error instanceof Error ? error : new Error(String(error)));
    }
  });
}
