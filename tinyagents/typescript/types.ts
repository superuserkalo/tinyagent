import { Ajv2020, type ValidateFunction } from "ajv/dist/2020.js";

export type Item = Record<string, unknown>;
export type Transport = (payload: Item) => Promise<Item[]> | Item[];
export type EventTransport = (payload: Item, signal: AbortSignal) => AsyncIterable<Item>;
export type OpenAIEventTransport = EventTransport;

/** One piece of a prompt or of a tool's result. An image is base64 `data` with its `mediaType`, or a `url`. */
export type ContentPart =
  | { type: "text"; text: string }
  | { type: "image"; mediaType: string; data: string }
  | { type: "image"; url: string };

/** Text, or text and images in order. */
export type Content = string | ContentPart[];

export function isContentParts(value: unknown): value is ContentPart[] {
  return Array.isArray(value) && value.length > 0 && value.every((part: unknown) => isItem(part) &&
    ((part.type === "text" && typeof part.text === "string") ||
     (part.type === "image" && (typeof part.url === "string" ||
       (typeof part.data === "string" && typeof part.mediaType === "string")))));
}

/** An image part as one URL: its own, or a data URL built from its base64 data. */
export function imageUrl(part: Item): string {
  return typeof part.url === "string" ? part.url : `data:${String(part.mediaType)};base64,${String(part.data)}`;
}

/** What a tool handler is told about the call it serves. */
export interface ToolContext {
  /** Aborts when the run is cancelled or the tool's timeout expires. */
  signal: AbortSignal;
  /** Stable across a rerun of the same call: "<sessionId>:<intent sequence>", or the call ID outside a Session. */
  executionId: string;
  /** The host value passed as the run's `context` option. Never journaled or sent to the model. */
  context: unknown;
  /** True once the tool's `timeoutSeconds` has expired: the signal fired for that, not for a cancelled run. */
  readonly timedOut: boolean;
}

/** The recommended form is `(args, signal, { executionId }) => ...`; the last two parameters are optional. */
export type ToolHandler = (arguments_: Item, signal: AbortSignal, context: ToolContext) => unknown | Promise<unknown>;

export interface ToolCall {
  callId: string;
  name: string;
  arguments: Item;
}

/** Returned by a handler or a beforeTool hook to stop the run until its Session is resumed with this call's result. */
export class Pause {
  readonly payload: unknown;

  constructor(payload: unknown) {
    this.payload = payload;
  }
}

/**
 * End the run here and wait for someone else to supply this tool call's
 * result: `return pause({ question: "Deploy to production?" })`. The payload
 * must be JSON; it is journaled and shown to whoever resumes.
 */
export function pause(payload: unknown = null): Pause {
  return new Pause(payload);
}

/** The tool call a paused run is waiting on. */
export interface PendingPause {
  toolCallId: string;
  toolName: string;
  payload: unknown;
}

/** A hook's verdict on a tool call: run it, refuse it with a reason, run it with other arguments, or pause. */
export type ToolDecision = undefined | { deny: string } | { arguments: Item } | Pause;
export type BeforeTool = (call: ToolCall, context: ToolContext) => ToolDecision | Promise<ToolDecision>;
/** A settled tool call as the model will see it. */
export interface ToolResult {
  output: Content;
  isError: boolean;
}

/**
 * Sees every tool call that ran, whether it succeeded or failed. Return
 * undefined to keep the result, a string to replace its output, or
 * `{ output, isError }` to replace both.
 */
export type AfterTool = (call: ToolCall, result: ToolResult, context: ToolContext) =>
  undefined | string | { output: Content; isError?: boolean } | Promise<undefined | string | { output: Content; isError?: boolean }>;

export interface ToolOptions {
  name: string;
  description: string;
  parameters: Item;
  handler: ToolHandler;
  /** True when running the same call twice is harmless. An interrupted idempotent call is rerun on resume. */
  idempotent?: boolean;
  /** Fire the handler's signal and report a tool error after this many seconds. */
  timeoutSeconds?: number;
  /** A beforeTool hook for this tool alone. It runs before the Agent's. */
  beforeTool?: BeforeTool;
}

/** Throw this from a handler to give the model exactly this text as the tool error. */
export class ToolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ToolError";
  }
}

/** A handler that honours its signal gets this long to report what it has before the timeout is final. */
const TIMEOUT_GRACE_MS = 1000;
/** A longer timeout would overflow the timer and fire at once. */
export const MAX_TIMEOUT_SECONDS = 86_400;
/** After a run is cancelled, host code that does not stop is abandoned after this long. */
export const CANCEL_GRACE_MS = 1000;

/**
 * Wait for host code (a handler, a hook, a provider, an event callback), but
 * not for ever: once `signal` has fired, the wait ends with an AbortError
 * after the grace period whether or not that code has settled.
 */
export function guarded<T>(work: T | Promise<T>, signal: AbortSignal): Promise<T> {
  if (!isPromise(work)) return Promise.resolve(work);
  return new Promise<T>((resolve, reject) => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const give = () => { timer ??= setTimeout(() => reject(abortError()), CANCEL_GRACE_MS); };
    const settle = () => { clearTimeout(timer); signal.removeEventListener("abort", give); };
    signal.addEventListener("abort", give, { once: true });
    if (signal.aborted) give();
    work.then((value) => { settle(); resolve(value); }, (error: unknown) => { settle(); reject(error); });
  });
}

const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g;

/** Strings as they are stored and sent: lone surrogates become U+FFFD. Everything else is copied as it is. */
export function wellFormed<T>(value: T): T {
  if (typeof value === "string") return value.replace(LONE_SURROGATE, "\uFFFD") as T;
  if (Array.isArray(value)) return value.map(wellFormed) as T;
  if (isItem(value)) return Object.fromEntries(Object.entries(value).map(([key, item]) => [wellFormed(key), wellFormed(item)])) as T;
  return value;
}

/** Throws a TypeError for content nested deeper than a prompt has any reason to be. */
export function checkDepth(value: unknown, limit = 32): void {
  if (typeof value !== "object" || value === null) return;
  if (limit === 0) throw new TypeError("content is nested too deeply");
  for (const item of Array.isArray(value) ? value : Object.values(value)) checkDepth(item, limit - 1);
}

/** Run options are checked before anything starts. A wrong one is a TypeError and leaves nothing behind. */
export function checkRunOptions(options: RunOptions): void {
  if (options.signal !== undefined && !(options.signal instanceof AbortSignal)) throw new TypeError("signal must be an AbortSignal");
  for (const name of ["maxSteps", "maxSeconds", "maxTokens"] as const) {
    const limit = options[name];
    if (limit !== undefined && !(typeof limit === "number" && Number.isFinite(limit) && limit >= 0))
      throw new TypeError(`${name} must be a finite number that is not negative`);
  }
  if (options.outputSchema !== undefined && !isItem(options.outputSchema)) throw new TypeError("outputSchema must be an object");
  if (options.extra !== undefined && !isItem(options.extra)) throw new TypeError("extra must be an object");
  if ("error" in options && options.error !== undefined && typeof options.error !== "string") throw new TypeError("error must be a string");
}

/**
 * Run `work` with a signal that also fires after `seconds`. On expiry the call
 * fails with a ToolError: the one `work` throws if it stops promptly, or
 * "timed out after N s".
 */
export async function withTimeout<T>(seconds: number | undefined, signal: AbortSignal,
  work: (signal: AbortSignal) => T | Promise<T>): Promise<T> {
  if (seconds === undefined) return await work(signal);
  const expiry = new AbortController();
  const expired = new ToolError(`timed out after ${seconds} s`);
  let grace: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_resolve, reject) => {
    expiry.signal.addEventListener("abort", () => { grace = setTimeout(() => reject(expired), TIMEOUT_GRACE_MS); });
  });
  timeout.catch(() => {});
  const timer = setTimeout(() => expiry.abort(expired), Math.min(seconds, MAX_TIMEOUT_SECONDS) * 1000);
  try {
    const running = Promise.resolve().then(() => work(AbortSignal.any([signal, expiry.signal])));
    return await Promise.race([running, timeout]);
  } catch (error) {
    if (expiry.signal.aborted && !signal.aborted && !(error instanceof ToolError)) throw expired;
    throw error;
  } finally {
    clearTimeout(timer);
    clearTimeout(grace);
  }
}

export class Tool {
  readonly name: string;
  readonly description: string;
  readonly parameters: Item;
  readonly handler: ToolHandler;
  readonly idempotent: boolean;
  readonly timeoutSeconds: number | undefined;
  readonly beforeTool: BeforeTool | undefined;
  private readonly validate: ValidateFunction<unknown>;

  constructor(options: ToolOptions) {
    this.name = options.name;
    this.description = options.description;
    this.parameters = structuredClone(options.parameters);
    this.handler = options.handler;
    this.idempotent = options.idempotent ?? false;
    this.timeoutSeconds = options.timeoutSeconds;
    this.beforeTool = options.beforeTool;
    if (this.timeoutSeconds !== undefined && !(this.timeoutSeconds > 0 && Number.isFinite(this.timeoutSeconds)))
      throw new TypeError("timeoutSeconds must be a positive finite number");
    if (this.parameters.$async) throw new TypeError("Tool schemas must use synchronous validation");
    // One validator per tool: nothing is shared between tools or kept at module level.
    // Validation is synchronous and local. $ref never triggers a network request.
    this.validate = new Ajv2020({ strict: false, validateFormats: false, addUsedSchema: false, ownProperties: true })
      .compile<unknown>(this.parameters);
  }

  definition(): Item {
    return {
      type: "function",
      name: this.name,
      description: this.description,
      strict: true,
      parameters: this.parameters,
    };
  }

  /** Throws a TypeError naming the first schema mismatch. */
  check(arguments_: Item): void {
    if (!this.validate(arguments_)) {
      const error = this.validate.errors?.[0];
      throw new TypeError(
        `Invalid arguments for ${this.name}${error?.instancePath ?? ""}: ${error?.message ?? "schema mismatch"}`,
      );
    }
  }

  /** Validate, then call the handler. Without a timeout the handler's own return value is passed through. */
  execute(arguments_: Item, signal: AbortSignal, context: Partial<ToolContext> = {}): unknown | Promise<unknown> {
    this.check(arguments_);
    const call = (scoped: AbortSignal) => this.handler(arguments_, scoped, {
      executionId: context.executionId ?? "", context: context.context, signal: scoped,
      get timedOut() { return scoped.aborted && !signal.aborted && scoped.reason instanceof ToolError; },
    });
    return this.timeoutSeconds === undefined ? call(signal) : withTimeout(this.timeoutSeconds, signal, call);
  }
}

export interface ProviderEvent {
  /** `reasoning_delta` is the model's visible reasoning, where the API streams it. */
  type: "text_delta" | "reasoning_delta";
  delta: string;
}

export interface ProviderTurn {
  text: string;
  assistant: Item;
  output: Item[];
  toolCalls: ToolCall[];
  /** Token counts for this request, when the API reported them. */
  usage?: { inputTokens: number; outputTokens: number };
}

export interface ProviderRequest {
  model: string;
  instructions: string;
  history: Item[];
  tools: Tool[];
  signal: AbortSignal;
  /** Merged into the request body after everything else. */
  extra?: Item;
}

export interface Provider {
  stream(request: ProviderRequest): AsyncGenerator<ProviderEvent, ProviderTurn>;
}

/** Tokens and model requests, summed over one run. */
export interface Usage {
  inputTokens: number;
  outputTokens: number;
  requests: number;
}

/** Each is checked before a model request. Undefined means unlimited. */
export interface RunLimits {
  /** Model requests per run. */
  maxSteps?: number;
  /** Wall-clock seconds per run. */
  maxSeconds?: number;
  /** Input plus output tokens per run. */
  maxTokens?: number;
}

interface EventBase {
  runId: string;
  sequence: number;
  /**
   * The sequence of the journal record this event stands for. Events from
   * `session.events()` always have one. Live session events have one when a
   * record exists for them; text and reasoning deltas never do.
   */
  cursor?: number;
}

export type CompactionEventData =
  | { type: "compaction_start" }
  | { type: "compaction_end"; status: "completed"; changed: boolean }
  | { type: "compaction_end"; status: "failed" | "aborted"; error: string };

export interface RunFailure {
  code: string;
  message: string;
  retryable: boolean;
  limit?: "steps" | "seconds" | "tokens";
}

export type AgentEvent =
  | (EventBase & { type: "agent_start"; resumed?: true; repeated?: true })
  | (EventBase & { type: "model_request_start"; attempt: number })
  | (EventBase & { type: "model_request_end"; usage: { inputTokens: number; outputTokens: number } })
  | (EventBase & { type: "model_request_end"; error: { code: string; message: string }; willRetry: boolean })
  | (EventBase & { type: "reasoning_delta"; delta: string })
  | (EventBase & CompactionEventData)
  | (EventBase & { type: "text_delta"; delta: string })
  | (EventBase & { type: "tool_execution_start"; toolCallId: string; toolName: string; arguments: Item;
      executionId: string; rerun?: true; interrupted?: true; resumed?: true })
  | (EventBase & { type: "tool_execution_end"; toolCallId: string; toolName: string; output: Content; isError: boolean;
      paused?: true })
  | (EventBase & { type: "agent_end"; status: "completed"; text: string; usage: Usage; repeated?: true; value?: unknown })
  | (EventBase & { type: "agent_end"; status: "paused"; pause: PendingPause; usage: Usage })
  | (EventBase & { type: "agent_end"; status: "failed"; error: RunFailure; usage: Usage })
  | (EventBase & { type: "agent_end"; status: "aborted"; usage: Usage });

export interface RunResult {
  text: string;
  history: Item[];
  /** The provider's raw output items for the final turn. Empty for a resumed or repeated run. */
  output: Item[];
  /** With `outputSchema`: the validated object the model finished with. */
  value?: unknown;
  usage: Usage;
  /** Set when the run stopped at `pause()`. Nothing was committed; resume the Session to continue. */
  pause?: PendingPause;
  /** True when a prompt's messageId had already committed and nothing ran. */
  repeated?: boolean;
}

export interface RunOptions extends RunLimits {
  signal?: AbortSignal;
  runId?: string;
  onEvent?: (event: AgentEvent) => void | Promise<void>;
  /** Session runs only: a caller-chosen ID that makes a repeated prompt safe. */
  messageId?: string;
  /** A host value handed to tool handlers and hooks. Never journaled or sent to the model. */
  context?: unknown;
  /** Use this model for the run instead of the agent's. */
  model?: string;
  /** Use these instructions for the run instead of the agent's. */
  instructions?: string;
  /** Merged into each request body, over the provider's own `extra`. */
  extra?: Item;
  /**
   * A JSON Schema for an object. The model must finish by calling a tool
   * with arguments that match it; they become `result.value`.
   */
  outputSchema?: Item;
  /** Also emit `model_request_start`, `model_request_end` and `reasoning_delta` events. Overrides the agent's setting. */
  trace?: boolean;
  /** `session.resume` only: the result of the tool call a paused run is waiting on. */
  result?: unknown;
  /** `session.resume` only: fail the paused tool call with this message instead. */
  error?: string;
}

/** The unfinished part of an interrupted run, as its Session read it from the journal. */
export interface ResumeState {
  /** The run's journaled history items, starting with its prompt. */
  items: Item[];
  /** Tool calls of the last model turn that have no result yet. */
  calls: ToolCall[];
  /** Set when the first of `calls` was started and never finished. `answer` is its result, supplied to a paused run. */
  pending?: { sequence: number; rerun: boolean; answer?: { result: unknown } | { error: string } };
  /** True when nothing is left but to commit: the final reply, or the typed output, is already journaled. */
  final: boolean;
  /** The typed output a `final_output` call had delivered before the interruption. */
  value?: { value: unknown };
}

/** How a Session makes a run durable. `Agent.stream` works without it. The Session journals the prompt itself. */
export interface RunHooks {
  sessionId?: string;
  /** Append one journal record durably and return its sequence. */
  record?: (record: Item) => number | Promise<number>;
  /** Compact before a request if needed. `usage` is what the summary request cost. */
  prepare?: (history: Item[]) => AsyncGenerator<CompactionEventData, { history: Item[]; usage?: Usage }>;
  resume?: ResumeState;
  /** Calls host code: a tool handler or a beforeTool or afterTool hook. The Session uses it to know when that code calls it back. */
  host?: <T>(call: () => T) => T;
}

export class ProviderError extends Error {
  /** True only when the request failed before any of the response body arrived and may be sent again. */
  readonly retryable: boolean;
  /** Seconds the server asked the client to wait, from Retry-After. */
  readonly retryAfter: number | undefined;

  constructor(message: string, options: { retryable?: boolean; retryAfter?: number } = {}) {
    super(message);
    this.name = "ProviderError";
    this.retryable = options.retryable ?? false;
    this.retryAfter = options.retryAfter;
  }
}

/** The journal could not be written. The session that hit it stops; open it again. */
export class StorageError extends Error {
  readonly code = "storage_error";

  constructor(cause: unknown) {
    super(errorMessage(cause), { cause });
    this.name = "StorageError";
  }
}

/** A run the agent ended itself. `code` is `limit_exceeded`, `repeated_tool_errors`, `hook_error` or `output_missing`. */
export class RunError extends Error {
  readonly code: string;
  readonly limit: RunFailure["limit"];

  constructor(code: string, message: string, limit?: RunFailure["limit"]) {
    super(message);
    this.name = "RunError";
    this.code = code;
    this.limit = limit;
  }
}

export class ToolOutputLimitError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ToolOutputLimitError";
  }
}

export function isItem(value: unknown): value is Item {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isPromise<T>(value: T | Promise<T>): value is Promise<T> {
  return typeof (value as { then?: unknown } | null | undefined)?.then === "function";
}

/** The text of a stored assistant message, whichever of the two shapes it has. */
export function assistantText(item: Item): string {
  if (typeof item.content === "string") return item.content;
  if (!Array.isArray(item.content)) return "";
  return item.content.map((block: unknown) => isItem(block) && block.type === "text" && typeof block.text === "string"
    ? block.text : "").join("");
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function abortError(): Error {
  const error = new Error("run aborted");
  error.name = "AbortError";
  return error;
}

export function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

export function throwIfAborted(signal: AbortSignal): void {
  if (signal.aborted) throw abortError();
}

// What follows stands in for Node APIs, so that everything a browser can run imports nothing from Node.

/** The length of `text` in UTF-8 bytes, a lone surrogate counting as U+FFFD: what Buffer.byteLength returns. */
export function utf8Length(text: string): number {
  let bytes = 0;
  for (let index = 0; index < text.length; index++) {
    const unit = text.charCodeAt(index);
    if (unit < 0x80) bytes += 1;
    else if (unit < 0x800) bytes += 2;
    else if (unit >= 0xd800 && unit <= 0xdbff && index + 1 < text.length &&
        text.charCodeAt(index + 1) >= 0xdc00 && text.charCodeAt(index + 1) <= 0xdfff) {
      bytes += 4;
      index += 1;
    } else bytes += 3;
  }
  return bytes;
}

/** The first `length` bytes of `parts` joined into one array. */
export function concatBytes(parts: Uint8Array[], length: number): Uint8Array {
  const joined = new Uint8Array(length);
  let offset = 0;
  for (const part of parts) {
    if (offset >= length) break;
    const piece = part.subarray(0, length - offset);
    joined.set(piece, offset);
    offset += piece.length;
  }
  return joined;
}

/** An environment variable, where the runtime has them. A browser has none. */
export function environment(name: string): string | undefined {
  return (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env?.[name];
}

/** 32 random hex digits: a version 4 UUID without its dashes. */
export function randomId(): string {
  // getRandomValues, unlike randomUUID, also works in a page that is not served over HTTPS.
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** Wait `milliseconds`, or reject with an AbortError as soon as `signal` fires. */
export function sleep(milliseconds: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(abortError());
    const abort = () => {
      clearTimeout(timer);
      reject(abortError());
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", abort);
      resolve();
    }, milliseconds);
    signal?.addEventListener("abort", abort, { once: true });
  });
}
