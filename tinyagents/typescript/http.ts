import { timingSafeEqual } from "node:crypto";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { isIP } from "node:net";
import { Session, RecoveryRequiredError, ResumeError, SessionFailedError, type ContextBudget, type SessionOptions } from "./session.ts";
import { FencedError, FileStorage, SessionBusyError, SessionCorruptionError, type Storage } from "./storage.ts";
import { isItem, isContentParts, isAbortError, errorMessage, RunError, StorageError } from "./types.ts";
import type { Agent, AgentEvent, Content, Item } from "./sdk.ts";
import { MAX_HTTP_BODY_BYTES, MAX_JSONL_LINE_BYTES, MAX_QUEUED_OUTPUT_BYTES, PROTOCOL_VERSION } from "./protocol.ts";

export interface ServerOptions {
  host?: string;
  port?: number;
  token?: string;
  allowRemote?: boolean;
  /** Where sessions are kept. Without `storage` or `directory` they live in memory. */
  storage?: Storage;
  directory?: string;
  budget?: ContextBudget;
  onUncertain?: SessionOptions["onUncertain"];
  /** Host names, besides the bound address, that requests may be addressed to. */
  allowedHosts?: string[];
  /** Origins whose pages may call the server. Default: none, so every request with an Origin header is refused. */
  allowedOrigins?: string[];
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

/** A connection that is shown a run it does not own: it never slows the run down and never cancels it. */
interface Follower {
  response: ServerResponse;
  /** The journal position its replay reached. Live events the journal already held then are not sent again. */
  after: number;
}

interface ActiveRun {
  /** Known once the run's first event arrives. */
  runId: string | null;
  controller: AbortController;
  done: Promise<void>;
  followers: Set<Follower>;
  /** Lines of the live events that no journal record covers yet, for a client that joins mid-turn. */
  buffer: string[];
  bufferBytes: number;
  /** The journal position `buffer` was collected at. */
  cursor: number;
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

  if (options.storage && options.directory !== undefined) throw new Error("provide either storage or directory, not both");
  const storage = options.storage ?? (options.directory !== undefined ? new FileStorage(options.directory) : undefined);
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
        : error instanceof ResumeError || error instanceof FencedError ? new HttpError(409, error.code, error.message)
        : error instanceof StorageError || error instanceof SessionFailedError
          ? new HttpError(409, "session_failed", "The session's storage failed. Open the session again: " + error.message)
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

  /** The name a request was addressed to, without its port. */
  const hostName = (header: string | undefined): string => {
    const value = (header ?? "").trim().toLowerCase();
    return value.startsWith("[") ? value.slice(1, value.indexOf("]")) : value.split(":")[0];
  };
  const allowedHosts = new Set([host, ...(options.allowedHosts ?? [])].map((name) => name.toLowerCase()));
  const allowedOrigins = new Set(options.allowedOrigins ?? []);

  async function route(request: IncomingMessage, response: ServerResponse): Promise<void> {
    // A page in a browser can reach a local server by a name it controls or as a cross-site request.
    // Both are refused before anything else is looked at.
    const name = hostName(request.headers.host);
    const local = loopback && (name === "localhost" || name === "::1" || (isIP(name) === 4 && name.startsWith("127.")));
    if (!local && !allowedHosts.has(name)) throw new HttpError(421, "misdirected_request", "Host is not this server");
    if (request.headers.origin !== undefined && !allowedOrigins.has(request.headers.origin))
      throw new HttpError(403, "origin_not_allowed", "Requests from this origin are not allowed");
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
        ...(storage ? await storage.list() : [])])].sort() });
      return;
    }
    if (request.method === "POST" && path === "/sessions") {
      const body = await readBody(request, false);
      if (closing) throw new HttpError(503, "server_closing", "Server is shutting down");
      const identifier = body?.sessionId;
      if (identifier !== undefined && (typeof identifier !== "string" || !storage))
        throw new HttpError(400, "invalid_request", "resume requires storage and a string sessionId");
      if (typeof identifier === "string" && sessions.has(identifier))
        throw new HttpError(409, "session_busy", "Session is already open");
      const core = new Session(agent, { storage, budget: options.budget, onUncertain: options.onUncertain,
        ...(typeof identifier === "string" ? { sessionId: identifier, resume: true } : {}) });
      await core.ready;
      if (closing || sessions.has(core.id)) {
        await core.close();
        throw new HttpError(409, "session_busy", "Session is already open");
      }
      sessions.set(core.id, { core, deleted: false });
      sendJson(response, 201, { sessionId: core.id });
      return;
    }
    const match = /^\/sessions\/([^/]+)(?:\/(prompts|resume|abandon|events|cancel|compact|reconcile))?$/.exec(path);
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
    } else if (request.method === "GET" && match[2] === "events") {
      const query = new URL(request.url ?? "", "http://localhost").searchParams;
      if ([...query.keys()].some((key) => query.getAll(key).length !== 1))
        throw new HttpError(400, "invalid_request", "Duplicate event options");
      const after = Number(query.get("after") ?? 0);
      if (query.get("follow") !== "1") {
        sendJson(response, 200, session.core.events({ after, limit: Number(query.get("limit") ?? 100) }));
        return;
      }
      // Replay, hand over the turn in progress, and subscribe, all without
      // yielding to the run in between, so no event is missed or sent twice.
      let page = session.core.events({ after, limit: 100 });
      response.writeHead(200, STREAM_HEADERS);
      while (true) {
        for (const event of page.events) response.write(`${JSON.stringify(event)}\n`);
        // The last page, the hand-over and the subscription happen in one turn, with nothing in between.
        if (page.nextAfter === null) break;
        // Earlier pages wait for the client to take what it was sent.
        if (response.writableNeedDrain) await new Promise<void>((resolve) => {
          const done = () => { response.off("drain", done); response.off("close", done); resolve(); };
          response.once("drain", done);
          response.once("close", done);
        });
        if (response.destroyed) return;
        if (session.deleted) { response.end(); return; }
        page = session.core.events({ after: page.nextAfter, limit: 100 });
      }
      const active = session.active;
      if (!active) {
        response.end();
        return;
      }
      const follower = { response, after: session.core.cursor };
      if (active.cursor === follower.after) for (const line of active.buffer) response.write(line);
      active.followers.add(follower);
      response.on("close", () => active.followers.delete(follower));
    } else if (request.method === "POST" && match[2] === "abandon") {
      const body = await readBody(request, false);
      if (session.active) throw new HttpError(409, "session_busy", "Session has an active run");
      if (session.deleted || closing) throw new HttpError(409, "session_closed", "Session is closing");
      if (body?.reason !== undefined && typeof body.reason !== "string")
        throw new HttpError(400, "invalid_request", "reason must be a string");
      await session.core.abandon(body?.reason);
      sendJson(response, 200, { abandoned: true });
    } else if (request.method === "POST" && match[2] === "reconcile") {
      const body = await readBody(request, true);
      if (session.active) throw new HttpError(409, "session_busy", "Session has an active run");
      if (session.deleted || closing) throw new HttpError(409, "session_closed", "Session is closing");
      if (!body || typeof body.note !== "string" || !body.note.trim())
        throw new HttpError(400, "invalid_request", "reconciliation requires a note");
      await session.core.reconcile(body.note);
      sendJson(response, 200, { recoveryRequired: false });
    } else if (request.method === "POST" && (match[2] === "prompts" || match[2] === "resume" || match[2] === "compact")) {
      const kind = match[2];
      const body = await readBody(request, kind === "prompts");
      if (kind === "prompts" && !(typeof body?.message === "string" ? body.message : isContentParts(body?.message)))
        throw new HttpError(400, "invalid_request", "message must be a non-empty string");
      if (body?.messageId !== undefined && (typeof body.messageId !== "string" || !body.messageId))
        throw new HttpError(400, "invalid_request", "messageId must be a non-empty string");
      if (body?.detach !== undefined && typeof body.detach !== "boolean")
        throw new HttpError(400, "invalid_request", "detach must be a boolean");
      if (session.deleted) throw new HttpError(404, "session_not_found", "Unknown session");
      if (closing) throw new HttpError(503, "server_closing", "Server is shutting down");
      if (session.core.recoveryRequired)
        throw new HttpError(409, "recovery_required", "Inspect history and reconcile interrupted tool effects");
      if (kind === "compact" && !options.budget) throw new HttpError(400, "budget_required", "Configure a compaction budget");
      if (session.active) {
        sendJson(response, 409, {
          error: { code: "session_busy", message: "The session already has an active run", retryable: true },
        });
        return;
      }
      const refused = await execute(session, kind, body ?? {}, response);
      if (refused !== undefined) throw refused;
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

  /**
   * Run one prompt, resume or compaction and write its events to `response`.
   * Resolves to the error when the session refused before the first event, so
   * the caller can answer with ordinary JSON.
   */
  async function execute(session: HttpSession, kind: string, body: Item, response: ServerResponse): Promise<unknown> {
    const detached = body.detach === true && kind !== "compact";
    const active: ActiveRun = { runId: null, controller: new AbortController(), done: Promise.resolve(),
      followers: new Set(), buffer: [], bufferBytes: 0, cursor: session.core.cursor };
    session.active = active;
    let streaming = false;
    let last: AgentEvent | undefined;
    // The line that ends the stream is held back until the session has recorded the outcome and is idle again.
    let held: { line: string; completed: boolean; cursor: number | undefined } | undefined;
    let failure: unknown;
    const disconnect = () => {
      if (!response.writableFinished) active.controller.abort();
    };
    // A detached run is shown to its own connection as to any other follower.
    const own = { response, after: 0 };
    if (detached) response.on("close", () => active.followers.delete(own));
    else {
      response.on("close", disconnect);
      response.on("error", disconnect);
    }
    const toFollowers = (line: string, cursor: number | undefined) => {
      for (const follower of active.followers) {
        // A follower dedupes on cursor: its replay already held everything up to `after`.
        if (cursor !== undefined && cursor <= follower.after) continue;
        // A follower that cannot keep up is dropped; the run goes on.
        if (follower.response.writableLength + line.length > MAX_QUEUED_OUTPUT_BYTES) follower.response.destroy();
        else if (!follower.response.destroyed && !follower.response.writableEnded) follower.response.write(line);
      }
    };
    const onEvent = async (event: AgentEvent) => {
      if (!streaming) {
        streaming = true;
        active.runId = event.runId;
        response.writeHead(200, STREAM_HEADERS);
        if (detached) active.followers.add(own);
      }
      last = event;
      let line = `${JSON.stringify(event)}\n`;
      // Compaction inside a prompt is an ordinary event. Only an explicit compaction ends with compaction_end.
      const terminal = kind === "compact" ? event.type === "compaction_end" : event.type === "agent_end";
      if (Buffer.byteLength(line) - 1 > MAX_JSONL_LINE_BYTES) {
        line = `${JSON.stringify({
          type: "agent_end",
          runId: event.runId,
          sequence: event.sequence,
          status: "failed",
          error: { code: "output_too_large", message: "Event exceeds the JSONL byte limit", retryable: false },
        })}\n`;
        held = { line, completed: false, cursor: undefined };
        active.controller.abort();
        throw new Error("Event exceeds the JSONL byte limit");
      }
      if (terminal) {
        held = { line, completed: "status" in event && event.status === "completed", cursor: event.cursor };
        return;
      }
      // A journal that has grown covers everything buffered before. An event whose
      // record is already written is found by a later replay and is not buffered.
      const cursor = session.core.cursor;
      if (cursor !== active.cursor) {
        active.cursor = cursor;
        active.buffer = [];
        active.bufferBytes = 0;
      }
      if (event.cursor === undefined) {
        active.buffer.push(line);
        active.bufferBytes += Buffer.byteLength(line);
        while (active.bufferBytes > MAX_QUEUED_OUTPUT_BYTES)
          active.bufferBytes -= Buffer.byteLength(active.buffer.shift() ?? "");
      }
      toFollowers(line, event.cursor);
      if (!detached) await writeLine(response, line, active.controller.signal, false);
    };
    active.done = (async () => {
      try {
        const runOptions = { signal: active.controller.signal, onEvent };
        if (kind === "compact") await session.core.compact({ ...runOptions, runId: `r_${agent.newId()}` });
        else if (kind === "resume") await session.core.resume({ ...runOptions,
          ...("result" in body ? { result: body.result } : {}),
          ...("error" in body ? { error: body.error as string } : {}) });
        else await session.core.run(body.message as Content,
          { ...runOptions, messageId: typeof body.messageId === "string" ? body.messageId : undefined });
      } catch (error) {
        failure = error ?? new Error("run failed");
        active.controller.abort();
      }
    })();
    await active.done;
    // The run is over and recorded: the session is idle before anyone is told so.
    session.active = undefined;
    response.off("close", disconnect);
    response.off("error", disconnect);
    if (!streaming || !last) return failure;
    // What the clients are told is what the journal holds: a run that did not commit did not complete.
    const cancelled = active.controller.signal.aborted || isAbortError(failure);
    const line = held && !(held.completed && failure !== undefined) ? held.line : `${JSON.stringify(kind === "compact"
      ? { type: "compaction_end", runId: last.runId, sequence: last.sequence + (held ? 0 : 1), status: "aborted",
          error: errorMessage(failure ?? "run aborted") }
      : { type: "agent_end", runId: last.runId, sequence: last.sequence + (held ? 0 : 1),
          usage: session.core.usage ?? { inputTokens: 0, outputTokens: 0, requests: 0 },
          ...(failure === undefined || cancelled ? { status: "aborted" } : { status: "failed", error: {
            code: failureCode(failure), message: errorMessage(failure), retryable: false } }) })}\n`;
    toFollowers(line, held && line === held.line ? held.cursor : undefined);
    for (const follower of active.followers) follower.response.end();
    if (!detached) {
      await writeLine(response, line, new AbortController().signal, true).catch(() => {});
      if (!response.writableFinished && !response.destroyed) response.destroy();
    }
    return undefined;
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

function failureCode(error: unknown): string {
  return isItem(error) && typeof error.code === "string" && (error instanceof FencedError || error instanceof StorageError ||
    error instanceof SessionFailedError || error instanceof RunError) ? error.code : "provider_error";
}

const STREAM_HEADERS = {
  "Content-Type": "application/jsonl; charset=utf-8",
  "Cache-Control": "no-store",
  "X-Content-Type-Options": "nosniff",
};

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
