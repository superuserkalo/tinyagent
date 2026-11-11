import type { Readable, Writable } from "node:stream";
import type { Agent } from "./agent.ts";
import { Session } from "./file_storage.ts";
import { ResumeError, SessionFailedError, type SessionOptions } from "./session.ts";
import { FencedError, SessionBusyError } from "./storage.ts";
import { errorMessage, isAbortError, isItem, isContentParts, RunError, StorageError, type Item, type AgentEvent,
  type Content } from "./types.ts";
import { PROTOCOL_VERSION, MAX_JSONL_LINE_BYTES, MAX_QUEUED_OUTPUT_BYTES } from "./protocol.ts";

/** A run's signal also lets a transport stop waiting for a stalled consumer. */
export type RpcEmitter = (value: object, signal?: AbortSignal) => void | Promise<void>;

interface ActiveRpcRun {
  /** Known once the run's first event arrives. */
  runId: string | null;
  controller: AbortController;
  done: Promise<void>;
}

export class RpcSession {
  readonly agent: Agent;
  private readonly emit: RpcEmitter;
  private readonly diagnostics: (message: string) => void;
  session: Session;
  private readonly options: SessionOptions;
  private active: ActiveRpcRun | undefined;
  private closing = false;

  constructor(
    agent: Agent,
    emit: RpcEmitter,
    diagnostics: (message: string) => void = (message) => console.error(message),
    options: SessionOptions = {},
  ) {
    this.agent = agent;
    this.emit = emit;
    this.diagnostics = diagnostics;
    this.options = options;
    this.session = new Session(agent, options);
  }

  async handle(request: Item): Promise<void> {
    const command = request.type;
    if ("id" in request && typeof request.id !== "string") {
      await this.emit({
        type: "protocol_error",
        error: { code: "invalid_command", message: "command.id must be a string" },
      });
      return;
    }
    const id = typeof request.id === "string" ? request.id : undefined;
    // No command is answered about a session whose storage is still loading.
    await this.session.ready.catch(() => {});
    if (command === "prompt" || command === "compact" || command === "resume_run") {
      if (this.closing) {
        await this.commandError(id, command, "session_closed", "RPC session is closed");
        return;
      }
      if (command === "prompt" && !(typeof request.message === "string" ? request.message : isContentParts(request.message))) {
        await this.commandError(id, "prompt", "invalid_command", "prompt.message must be a non-empty string");
        return;
      }
      if (request.messageId !== undefined && (typeof request.messageId !== "string" || !request.messageId)) {
        await this.commandError(id, command, "invalid_command", "messageId must be a non-empty string");
        return;
      }
      if (this.session.recoveryRequired || (command === "compact" && !this.options.budget)) {
        await this.commandError(id, command, "session_error", "Reconcile interrupted tools or configure a compaction budget");
        return;
      }
      if (this.active) {
        await this.commandError(id, command, "busy", "A run is already active");
        return;
      }
      // The response names the run, so it is written when the run's first event
      // arrives, or as an error if the session refuses to start one.
      let answer!: (failure?: unknown) => void;
      const answered = new Promise<void>((resolve, reject) => {
        answer = (failure) => (failure === undefined ? resolve() : reject(failure));
      });
      const active: ActiveRpcRun = { runId: null, controller: new AbortController(), done: Promise.resolve() };
      this.active = active;
      active.done = this.execute(active, id, command, request, answer);
      await answered;
    } else if (command === "abort") {
      const active = this.active;
      active?.controller.abort();
      await this.emit(
        rpcResponse(id, "abort", {
          accepted: active !== undefined,
          runId: active?.runId ?? null,
        }),
      );
    } else if (this.closing && (command === "new_session" || command === "resume_session")) {
      // A closed adapter must not open a journal that nothing will close.
      await this.commandError(id, command, "session_closed", "RPC session is closed");
    } else if (command === "new_session") {
      const active = this.active;
      if (active) {
        active.controller.abort();
        await active.done;
      }
      try {
        const created = new Session(this.agent, { ...this.options, sessionId: undefined, resume: false });
        await created.ready;
        await this.session.close();
        this.session = created;
      } catch (error) {
        // Every command gets an answer: a store that cannot create a session is reported, not thrown.
        await this.commandError(id, command, "session_error", errorMessage(error));
        return;
      }
      await this.emit(rpcResponse(id, "new_session", { abortedRun: active !== undefined }));
    } else if (command === "get_state") {
      await this.emit(
        rpcResponse(id, "get_state", {
          protocolVersion: PROTOCOL_VERSION,
          status: this.active ? "running" : "idle",
          runId: this.active?.runId ?? null,
          messageCount: this.session.history.length,
          resumable: this.session.resumable,
          paused: this.session.paused ?? null,
          usage: this.session.usage ?? null,
        }),
      );
    } else if (["list_sessions", "resume_session", "get_history", "get_events", "reconcile", "abandon_run"].includes(String(command))) {
      if (this.active) {
        await this.commandError(id, String(command), "busy", "A run is already active");
        return;
      }
      let data: Item;
      try {
        if (command === "list_sessions") {
          data = { sessionIds: this.options.storage ? await this.options.storage.list()
            : this.options.directory ? Session.list(this.options.directory) : [this.session.id] };
        } else if (command === "resume_session") {
          if ((!this.options.directory && !this.options.storage) || typeof request.sessionId !== "string")
            throw new Error("resume_session requires storage and sessionId");
          if (request.sessionId !== this.session.id) {
            const resumed = new Session(this.agent, { ...this.options, sessionId: request.sessionId, resume: true });
            await resumed.ready;
            await this.session.close();
            this.session = resumed;
          }
          data = { sessionId: this.session.id };
        } else if (command === "get_history") {
          if ((request.offset !== undefined && typeof request.offset !== "number") ||
              (request.limit !== undefined && typeof request.limit !== "number") ||
              (request.view !== undefined && typeof request.view !== "string"))
            throw new Error("invalid inspection options");
          data = this.session.inspect({ offset: request.offset, limit: request.limit, view: request.view });
        } else if (command === "get_events") {
          if ((request.after !== undefined && typeof request.after !== "number") ||
              (request.limit !== undefined && typeof request.limit !== "number"))
            throw new Error("invalid event options");
          data = { ...this.session.events({ after: request.after, limit: request.limit }) };
        } else if (command === "abandon_run") {
          if (request.reason !== undefined && typeof request.reason !== "string") throw new Error("reason must be a string");
          if (!this.session.resumable && !this.session.recoveryRequired) {
            await this.commandError(id, command, "nothing_to_resume", "session has no interrupted run");
            return;
          }
          await this.session.abandon(request.reason);
          data = { abandoned: true };
        } else {
          if (typeof request.note !== "string") throw new Error("reconciliation requires a note");
          await this.session.reconcile(request.note);
          data = { sessionId: this.session.id, recoveryRequired: false };
        }
      } catch (error) {
        const code = error instanceof StorageError || error instanceof SessionFailedError || error instanceof FencedError
          ? error.code : "session_error";
        await this.commandError(id, String(command), code, errorMessage(error));
        return;
      }
      await this.emit(rpcResponse(id, String(command), data));
    } else {
      await this.commandError(
        id,
        typeof command === "string" ? command : "unknown",
        "invalid_command",
        "Unknown command type",
      );
    }
  }

  async waitIdle(timeoutMs?: number): Promise<boolean> {
    const active = this.active;
    if (!active) return true;
    if (timeoutMs === undefined) {
      await active.done;
      return this.active === undefined;
    }
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<false>((resolve) => (timer = setTimeout(() => resolve(false), timeoutMs)));
    const idle = active.done.then(() => true);
    const result = await Promise.race([idle, timeout]);
    if (timer) clearTimeout(timer);
    return result && this.active === undefined;
  }

  async close(): Promise<void> {
    this.closing = true;
    await this.stop();
    await this.session.close();
  }

  async stop(): Promise<void> {
    const active = this.active;
    if (active) {
      active.controller.abort();
      await active.done;
    }
  }

  private async execute(active: ActiveRpcRun, id: string | undefined, command: string, request: Item,
    answer: (failure?: unknown) => void): Promise<void> {
    let answered = false;
    let last: AgentEvent | undefined;
    // The event that ends the run is held back until the session has recorded the outcome and is idle again.
    let held: AgentEvent | undefined;
    let failure: unknown;
    try {
      const options = {
        signal: active.controller.signal,
        onEvent: async (event: AgentEvent) => {
          last = event;
          if (event.type === "agent_end" || event.type === "compaction_end") held = event;
          try {
            if (!answered) {
              answered = true;
              active.runId = event.runId;
              try {
                await this.emit(rpcResponse(id, command, { runId: event.runId,
                  ...(command === "prompt" ? { repeated: event.type === "agent_start" && event.repeated === true } : {}) }),
                  active.controller.signal);
              } catch (error) {
                answer(error);
                throw error;
              }
              answer();
            }
            if (held !== event) await this.emit(event, active.controller.signal);
          } catch (error) {
            active.controller.abort();
            throw error;
          }
        },
      };
      if (command === "compact") await this.session.compact({ ...options, runId: `r_${this.agent.newId()}` });
      else if (command === "resume_run") await this.session.resume({ ...options,
        ...("result" in request ? { result: request.result } : {}),
        ...("error" in request ? { error: request.error as string } : {}) });
      else await this.session.run(request.message as Content,
        { ...options, messageId: typeof request.messageId === "string" ? request.messageId : undefined });
    } catch (error) {
      failure = error;
    }
    if (this.active === active) this.active = undefined;
    try {
      if (!answered) {
        // The session refused before any event: the command fails, and no run existed.
        await this.commandError(id, command, errorCode(failure, true), errorMessage(failure));
      } else if (last) {
        // What the client is told is what the journal holds: a run that did not commit did not complete.
        const event = last;
        const reported = held && !(failure !== undefined && "status" in held && held.status === "completed") ? held
          : command === "compact" ? { type: "compaction_end", runId: event.runId, sequence: event.sequence + (held ? 0 : 1),
              status: "aborted", error: errorMessage(failure ?? "run aborted") }
          : { type: "agent_end", runId: event.runId, sequence: event.sequence + (held ? 0 : 1),
              usage: this.session.usage ?? { inputTokens: 0, outputTokens: 0, requests: 0 },
              ...(failure === undefined || active.controller.signal.aborted ? { status: "aborted" }
                : { status: "failed", error: { code: errorCode(failure, false), message: errorMessage(failure), retryable: false } }) };
        await this.emit(reported);
        if (failure !== undefined && !active.controller.signal.aborted) this.diagnostics(`run ${active.runId} failed: ${errorMessage(failure)}`);
      }
    } catch {
      // The output is gone; there is nobody left to tell.
    } finally {
      answer();
    }
  }

  private async commandError(id: string | undefined, command: string, code: string, message: string): Promise<void> {
    const error = { code, message };
    if (id === undefined) await this.emit({ type: "protocol_error", error });
    else await this.emit({ id, type: "response", command, success: false, error });
  }
}

class RpcOutputLimitError extends Error {}
class RpcCancelledWriteError extends Error {}

class RpcInputError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

class RpcJsonlWriter {
  private readonly output: Writable;
  private readonly stopping: AbortSignal;
  private readonly cancelRun: () => void;
  private pending: Promise<void> = Promise.resolve();
  private queuedBytes = 0;

  constructor(output: Writable, stopping: AbortSignal, cancelRun: () => void) {
    this.output = output;
    this.stopping = stopping;
    this.cancelRun = cancelRun;
  }

  write = async (value: object, signal?: AbortSignal): Promise<void> => {
    const data: Buffer = Buffer.from(`${JSON.stringify(value)}\n`, "utf8");
    if (data.length - 1 > MAX_JSONL_LINE_BYTES) {
      await this.rejectOutput(
        "output_too_large",
        `RPC output exceeds ${MAX_JSONL_LINE_BYTES} UTF-8 bytes`,
        "RPC output line is too large",
      );
      return;
    }
    if (this.queuedBytes + data.length > MAX_QUEUED_OUTPUT_BYTES) {
      await this.rejectOutput(
        "output_queue_full",
        `RPC output queue exceeds ${MAX_QUEUED_OUTPUT_BYTES} bytes`,
        "RPC output queue is full",
      );
      return;
    }
    this.queuedBytes += data.length;
    try {
      await this.enqueue(data, signal);
    } finally {
      this.queuedBytes -= data.length;
    }
  };

  private async rejectOutput(code: string, message: string, failure: string): Promise<never> {
    this.cancelRun();
    const error = Buffer.from(`${JSON.stringify({ type: "protocol_error", error: { code, message } })}\n`, "utf8");
    await this.enqueue(error, AbortSignal.abort());
    throw new RpcOutputLimitError(failure);
  }

  private async enqueue(data: Buffer, signal?: AbortSignal): Promise<void> {
    const cancellation = signal ? AbortSignal.any([signal, this.stopping]) : this.stopping;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const cancel = () => {
      timer ??= setTimeout(() => {
        this.output.destroy(new RpcCancelledWriteError("RPC output did not drain after cancellation"));
      }, 1000);
    };
    cancellation.addEventListener("abort", cancel, { once: true });
    if (cancellation.aborted) cancel();
    const operation = this.pending.then(() => this.writeRaw(data));
    this.pending = operation.catch(() => {});
    try {
      await operation;
    } finally {
      cancellation.removeEventListener("abort", cancel);
      clearTimeout(timer);
    }
  }

  private writeRaw(data: Buffer): Promise<void> {
    return new Promise((resolve, reject) => {
      const closed = () => finish(new Error("RPC output closed"));
      const finish = (error?: Error | null) => {
        this.output.off("close", closed);
        this.output.off("error", finish);
        if (error) reject(error);
        else resolve();
      };
      if (this.output.destroyed || this.output.writableEnded) {
        finish(this.output.errored ?? new Error("RPC output closed"));
        return;
      }
      this.output.once("close", closed);
      this.output.once("error", finish);
      this.output.write(data, finish);
    });
  }
}

export async function runRpc(
  agent: Agent,
  input: Readable = process.stdin,
  output: Writable = process.stdout,
  options: SessionOptions = {},
): Promise<void> {
  if (output.destroyed || output.writableEnded) throw new Error("RPC output closed");
  const stopping = new AbortController();
  const cancelRun = () => {
    void session.stop().catch(() => {});
  };
  const writer = new RpcJsonlWriter(output, stopping.signal, cancelRun);
  const session = new RpcSession(agent, writer.write, undefined, options);
  let outputFailure: Error | undefined;
  const failed = (error: Error) => {
    if (outputFailure) return;
    outputFailure = error;
    stopping.abort();
    input.destroy(error);
    cancelRun();
  };
  const closed = () => failed(new Error("RPC output closed"));
  output.on("error", failed);
  output.on("close", closed);
  output.on("finish", closed);
  let interrupted = false;
  const end = () => stopping.abort();
  input.on("end", end);
  const interrupt = () => {
    interrupted = true;
    stopping.abort();
    cancelRun();
    input.destroy();
  };
  if (input === process.stdin) process.on("SIGINT", interrupt);
  try {
    for await (const line of rpcLines(input)) {
      let request: unknown;
      try {
        request = JSON.parse(line);
      } catch {
        await writer.write({
          type: "protocol_error",
          error: { code: "invalid_json", message: "RPC input is not valid JSON" },
        });
        continue;
      }
      if (!isItem(request)) {
        await writer.write({
          type: "protocol_error",
          error: { code: "invalid_command", message: "RPC command must be an object" },
        });
        continue;
      }
      try {
        await session.handle(request);
      } catch (error) {
        if (!(error instanceof RpcOutputLimitError)) throw error;
        // The writer already emitted the bounded error. Cancel the run and
        // continue reading commands while the output transport is usable.
        await session.stop();
      }
    }
  } catch (error) {
    if (outputFailure instanceof RpcCancelledWriteError) {
      // Cancellation closed a transport that could no longer carry events.
    } else if (outputFailure) {
      throw outputFailure;
    } else if (error instanceof RpcInputError) {
      stopping.abort();
      cancelRun();
      try {
        await writer.write({ type: "protocol_error", error: { code: error.code, message: error.message } });
      } catch (writeError) {
        if (!(writeError instanceof RpcCancelledWriteError)) throw writeError;
      }
    } else if (!(error instanceof RpcOutputLimitError) && !interrupted && !isAbortError(error)) {
      throw error;
    }
  } finally {
    if (input === process.stdin) process.off("SIGINT", interrupt);
    input.off("end", end);
    stopping.abort();
    await session.close();
    await session.session.close();
    output.off("error", failed);
    output.off("close", closed);
    output.off("finish", closed);
  }
  if (outputFailure && !(outputFailure instanceof RpcCancelledWriteError)) throw outputFailure;
}

/** The standalone RPC process owns exit; the imported stream driver does not. */
export async function runRpcProcess(agent: Agent, options: SessionOptions = {}): Promise<never> {
  await runRpc(agent, process.stdin, process.stdout, options);
  // Node deliberately keeps stdout's descriptor open after destroy(). Await
  // provider/tool cleanup above before exiting with any cancelled write pending.
  process.exit(0);
}

async function* rpcLines(input: Readable): AsyncGenerator<string> {
  let buffer = Buffer.alloc(0);
  const decoder = new TextDecoder("utf-8", { fatal: true });
  for await (const chunk of input as AsyncIterable<unknown>) {
    buffer = Buffer.concat([buffer, Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk))]);
    while (true) {
      const newline = buffer.indexOf(0x0a);
      if (newline < 0) break;
      let line = buffer.subarray(0, newline);
      buffer = buffer.subarray(newline + 1);
      if (line.length > MAX_JSONL_LINE_BYTES)
        throw new RpcInputError("line_too_large", `RPC input exceeds ${MAX_JSONL_LINE_BYTES} UTF-8 bytes`);
      try {
        yield decoder.decode(line);
      } catch {
        throw new RpcInputError("invalid_utf8", "RPC input is not valid UTF-8");
      }
    }
    if (buffer.length > MAX_JSONL_LINE_BYTES)
      throw new RpcInputError("line_too_large", `RPC input exceeds ${MAX_JSONL_LINE_BYTES} UTF-8 bytes`);
  }
  if (buffer.length) {
    try {
      yield decoder.decode(buffer);
    } catch {
      throw new RpcInputError("invalid_utf8", "RPC input is not valid UTF-8");
    }
  }
}

/** The name of a failure on the wire. `refusal` is true when no run had started. */
function errorCode(error: unknown, refusal: boolean): string {
  if (error instanceof SessionBusyError) return "busy";
  if (isItem(error) && typeof error.code === "string" && (error instanceof ResumeError || error instanceof FencedError ||
      error instanceof StorageError || error instanceof SessionFailedError || error instanceof RunError)) return error.code;
  // A malformed command is invalid_command over RPC, as it has been since version 1.
  if (refusal) return error instanceof TypeError ? "invalid_command" : "session_error";
  return "provider_error";
}

function rpcResponse(id: string | undefined, command: string, data: Item): Item {
  return {
    ...(id === undefined ? {} : { id }),
    type: "response",
    command,
    success: true,
    data,
  };
}
