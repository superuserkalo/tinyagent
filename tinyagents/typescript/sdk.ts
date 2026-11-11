import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { createInterface } from "node:readline";
import type { Readable, Writable } from "node:stream";
import { setTimeout as delay } from "node:timers/promises";
import { runRpcProcess } from "./rpc.ts";
import { serve } from "./http.ts";
import {
  Tool,
  Pause,
  ProviderError,
  RunError,
  ToolError,
  ToolOutputLimitError,
  assistantText,
  errorMessage,
  abortError,
  throwIfAborted,
  isItem,
  guarded,
  wellFormed,
  checkDepth,
  checkRunOptions,
  StorageError,
  CANCEL_GRACE_MS,
  type Item,
  type PendingPause,
  type Content,
  type ProviderEvent,
  isContentParts,
  type Transport,
  type Provider,
  type ProviderRequest,
  type ProviderTurn,
  type AgentEvent,
  type RunResult,
  type RunOptions,
  type RunLimits,
  type RunHooks,
  type ToolCall,
  type ToolContext,
  type BeforeTool,
  type AfterTool,
  type Usage,
} from "./types.ts";
import { OpenAIProvider, CompleteTransportProvider } from "./openai.ts";
import { AnthropicProvider } from "./anthropic.ts";
import { ChatCompletionsProvider } from "./chat.ts";
import { Session, type ContextBudget } from "./session.ts";
import { FencedError, FileStorage } from "./storage.ts";
import { bashTool, deniedReason, type Approve } from "./bash.ts";
import { MAX_TOOL_OUTPUT_BYTES } from "./protocol.ts";

export { Session, RecoveryRequiredError, ResumeError, SessionFailedError } from "./session.ts";
export type { ContextBudget, SessionOptions, EventPage } from "./session.ts";
export { SessionBusyError, SessionCorruptionError, FencedError, FileStorage, MemoryStorage, checkStorage } from "./storage.ts";
export type { Storage } from "./storage.ts";
export { bashTool, runBash, deniedReason } from "./bash.ts";
export type { BashOptions, Approve } from "./bash.ts";

export { Tool, Pause, pause, ProviderError, RunError, StorageError, ToolError, ToolOutputLimitError } from "./types.ts";
export type {
  Item,
  Transport,
  EventTransport,
  OpenAIEventTransport,
  ToolHandler,
  ToolContext,
  ToolOptions,
  ToolCall,
  ToolDecision,
  PendingPause,
  Content,
  ContentPart,
  BeforeTool,
  AfterTool,
  ToolResult,
  ProviderEvent,
  ProviderTurn,
  ProviderRequest,
  Provider,
  AgentEvent,
  RunFailure,
  RunResult,
  RunOptions,
  RunLimits,
  RunHooks,
  ResumeState,
  Usage,
} from "./types.ts";
export { OpenAIProvider } from "./openai.ts";
export type { OpenAIProviderOptions } from "./openai.ts";
export { AnthropicProvider } from "./anthropic.ts";
export type { AnthropicProviderOptions } from "./anthropic.ts";
export { ChatCompletionsProvider } from "./chat.ts";
export type { ChatCompletionsProviderOptions } from "./chat.ts";

export { ScriptedProvider } from "./scripted.ts";
export type { ScriptedTurn } from "./scripted.ts";

export { RpcSession, runRpc } from "./rpc.ts";
export type { RpcEmitter } from "./rpc.ts";

export { serve } from "./http.ts";
export type { AgentServer, ServerOptions } from "./http.ts";
export {
  PROTOCOL_VERSION,
  MAX_JSONL_LINE_BYTES,
  MAX_HTTP_BODY_BYTES,
  MAX_QUEUED_OUTPUT_BYTES,
  MAX_TOOL_OUTPUT_BYTES,
} from "./protocol.ts";

export const VERSION = "0.2.0";

const INSTRUCTIONS = `You are TinyAgent, a coding agent in the current directory.
Inspect before changing files. Use bash for shell and file work. Each call is a fresh process.`;
/** A model request is sent at most this many times. */
const MAX_ATTEMPTS = 4;
const MAX_RETRY_DELAY_SECONDS = 30;
/** A run ends after this many model turns in a row whose tool calls all failed. */
const MAX_ERROR_TURNS = 3;
/** The tool the model must call to finish a run that has an outputSchema. */
const OUTPUT_TOOL = "final_output";
const INTERRUPTED = "This tool call was interrupted and may have partly run. Check its effects before continuing.";

export interface AgentOptions extends RunLimits {
  model: string;
  instructions?: string;
  tools?: Tool[];
  apiKey?: string;
  provider?: Provider;
  transport?: Transport;
  /** Runs before every tool call, after its arguments are validated. */
  beforeTool?: BeforeTool;
  /** Runs after every successful tool call and may replace its result. */
  afterTool?: AfterTool;
  /** The random part of run and session IDs. Default: a UUID without dashes. */
  newId?: () => string;
  /** Also emit `model_request_start`, `model_request_end` and `reasoning_delta` events. Default false. */
  trace?: boolean;
  /** Waits between provider retries. It must reject or return when the signal aborts. */
  sleep?: (milliseconds: number, signal: AbortSignal) => Promise<unknown>;
}

export class Agent {
  readonly model: string;
  readonly instructions: string;
  readonly tools: Map<string, Tool>;
  readonly provider: Provider;
  readonly limits: RunLimits;
  readonly beforeTool: BeforeTool | undefined;
  readonly afterTool: AfterTool | undefined;
  readonly newId: () => string;
  readonly trace: boolean;
  private readonly sleep: (milliseconds: number, signal: AbortSignal) => Promise<unknown>;

  constructor(options: AgentOptions) {
    if (options.provider && options.transport) throw new Error("provide either provider or transport, not both");
    this.model = options.model;
    this.instructions = options.instructions ?? "";
    this.tools = new Map((options.tools ?? []).map((tool) => [tool.name, tool]));
    if (this.tools.size !== (options.tools ?? []).length) throw new Error("tool names must be unique");
    this.provider =
      options.provider ??
      (options.transport
        ? new CompleteTransportProvider(options.transport)
        : new OpenAIProvider({ apiKey: options.apiKey }));
    this.limits = { maxSteps: options.maxSteps, maxSeconds: options.maxSeconds, maxTokens: options.maxTokens };
    this.beforeTool = options.beforeTool;
    this.afterTool = options.afterTool;
    this.trace = options.trace ?? false;
    this.newId = options.newId ?? (() => randomUUID().replaceAll("-", ""));
    this.sleep = options.sleep ?? ((milliseconds, signal) => delay(milliseconds, undefined, { signal }));
  }

  /**
   * The agent loop: ask the model, run the tools it calls, repeat until it
   * answers without calling one. `hooks` is how a Session journals the run and
   * continues an interrupted one; everything else leaves it out.
   */
  async *stream(
    prompt: Content,
    history: Item[] = [],
    options: Omit<RunOptions, "onEvent" | "messageId"> = {},
    hooks: RunHooks = {},
  ): AsyncGenerator<AgentEvent, RunResult> {
    checkRunOptions(options);
    checkDepth(prompt);
    const controller = new AbortController();
    const signal = options.signal ? AbortSignal.any([options.signal, controller.signal]) : controller.signal;
    const resume = hooks.resume;
    let transcript = [...structuredClone(history),
      ...(resume ? structuredClone(resume.items) : [{ role: "user", content: wellFormed(prompt) }])];
    const runId = options.runId ?? `r_${this.newId()}`;
    const maxSteps = options.maxSteps ?? this.limits.maxSteps;
    const maxSeconds = options.maxSeconds ?? this.limits.maxSeconds;
    const maxTokens = options.maxTokens ?? this.limits.maxTokens;
    const trace = options.trace ?? this.trace;
    const started = Date.now();
    const usage: Usage = { inputTokens: 0, outputTokens: 0, requests: 0 };
    let sequence = 0;
    let steps = 0;
    let terminal = false;
    const event = <T extends Omit<AgentEvent, "runId" | "sequence">>(value: T): AgentEvent =>
      ({ ...value, runId, sequence: ++sequence }) as AgentEvent;
    /** Journal one record. A cancellation that arrived while it was being written is noticed here. */
    const record = async (value: Item): Promise<number | undefined> => {
      const written = await hooks.record?.(value);
      throwIfAborted(signal);
      return written;
    };
    // What is left of the last model turn: tool calls without a result, or the final reply.
    let calls: ToolCall[] = structuredClone(resume?.calls ?? []);
    let pending = resume?.pending;
    let reply: { text: string; output: Item[]; value?: unknown } | undefined = !resume?.final ? undefined
      : { text: resume.value ? "" : assistantText(resume.items.at(-1) ?? {}), output: [], ...resume.value };
    let errorTurns = 0;
    // With an outputSchema the run finishes through a tool whose parameters are that schema.
    const tools = new Map(this.tools);
    if (options.outputSchema) {
      if (tools.has(OUTPUT_TOOL)) throw new Error(`outputSchema needs the tool name ${OUTPUT_TOOL}`);
      tools.set(OUTPUT_TOOL, new Tool({ name: OUTPUT_TOOL, parameters: options.outputSchema, handler: () => "Output accepted.",
        description: "Give the final answer. Call this exactly once, when the task is complete." }));
    }
    let nudged = false;

    try {
      yield event({ type: "agent_start", ...(resume ? { resumed: true } : {}) });
      while (true) {
        throwIfAborted(signal);
        if (!calls.length && !reply) {
          if (maxSteps !== undefined && steps >= maxSteps)
            throw new RunError("limit_exceeded", `run exceeded maxSteps (${maxSteps})`, "steps");
          if (maxSeconds !== undefined && (Date.now() - started) / 1000 >= maxSeconds)
            throw new RunError("limit_exceeded", `run exceeded maxSeconds (${maxSeconds})`, "seconds");
          if (maxTokens !== undefined && usage.inputTokens + usage.outputTokens >= maxTokens)
            throw new RunError("limit_exceeded", `run exceeded maxTokens (${maxTokens})`, "tokens");
          if (hooks.prepare) {
            const preparation = hooks.prepare(transcript);
            try {
              while (true) {
                const next = await guarded(preparation.next(), signal);
                if (next.done) {
                  transcript = next.value.history;
                  // The summary request is part of what this run cost.
                  usage.requests += next.value.usage?.requests ?? 0;
                  usage.inputTokens += next.value.usage?.inputTokens ?? 0;
                  usage.outputTokens += next.value.usage?.outputTokens ?? 0;
                  break;
                }
                yield event(next.value);
              }
            } finally { await bounded(preparation.return(undefined as never)); }
            throwIfAborted(signal);
          }
          steps += 1;
          let turn: ProviderTurn | undefined;
          for (let attempt = 1; !turn; attempt++) {
            const deltas = this.request({ model: options.model ?? this.model, instructions: options.instructions ?? this.instructions,
              history: transcript, tools: [...tools.values()], signal, ...(options.extra ? { extra: options.extra } : {}) },
              () => controller.abort());
            let streamed = false;
            if (trace) yield event({ type: "model_request_start", attempt });
            try {
              while (true) {
                const next = await deltas.next();
                if (next.done) { turn = next.value; break; }
                streamed = true;
                if (next.value.type === "text_delta" || trace) yield event(next.value);
              }
            } catch (error) {
              // Retry only a request that failed before any of its response arrived.
              const willRetry = error instanceof ProviderError && error.retryable && !streamed && attempt < MAX_ATTEMPTS &&
                !signal.aborted;
              if (trace) yield event({ type: "model_request_end", willRetry, error: {
                code: signal.aborted ? "aborted" : "provider_error", message: signal.aborted ? "run aborted" : errorMessage(error) } });
              if (!willRetry || !(error instanceof ProviderError)) throw error;
              await guarded(this.sleep(Math.min(error.retryAfter ?? 2 ** (attempt - 1), MAX_RETRY_DELAY_SECONDS) * 1000, signal), signal);
              throwIfAborted(signal);
            } finally { await bounded(deltas.return(undefined as never)); }
          }
          if (trace) yield event({ type: "model_request_end", usage: turn.usage ?? { inputTokens: 0, outputTokens: 0 } });
          usage.requests += 1;
          usage.inputTokens += turn.usage?.inputTokens ?? 0;
          usage.outputTokens += turn.usage?.outputTokens ?? 0;
          transcript.push(turn.assistant);
          // The loop works on its own copy: nothing a handler or a consumer does can reach the stored message.
          calls = structuredClone(turn.toolCalls);
          await record({ type: "item", item: turn.assistant, ...(calls.length ? { toolCalls: calls } : {}) });
          if (!calls.length && options.outputSchema) {
            // The model answered in text. It is told once what is expected; a second miss ends the run.
            if (nudged) throw new RunError("output_missing", `the model did not call ${OUTPUT_TOOL}`);
            nudged = true;
            const reminder = { role: "user", content: `Call the ${OUTPUT_TOOL} tool with your final answer.` };
            transcript.push(reminder);
            await record({ type: "item", item: reminder });
            continue;
          }
          if (!calls.length) reply = { text: turn.text, output: turn.output };
        }
        if (reply) {
          const result = { ...reply, history: transcript, usage: { ...usage } };
          terminal = true;
          yield event({ type: "agent_end", status: "completed", text: reply.text, usage: { ...usage },
            ...("value" in reply ? { value: reply.value } : {}) });
          throwIfAborted(signal);
          return result;
        }
        let failures = 0;
        let accepted: { value: Item } | undefined;
        // A turn that delivers the final output finishes the run: its other calls are answered, not run.
        const finishing = options.outputSchema !== undefined && calls.some((call) => call.name === OUTPUT_TOOL);
        for (const call of calls) {
          const tool = tools.get(call.name);
          // A call that was started before a crash keeps its intent record, and so its execution ID.
          const interrupted = pending;
          pending = undefined;
          const answer = interrupted?.answer;
          const intent = interrupted?.sequence ?? await record({ type: "tool_intent", callId: call.callId,
            toolName: call.name, arguments: call.arguments, idempotent: tool?.idempotent ?? false });
          // Running a call again, or answering a paused one, is journaled first, so a writer that
          // has been overtaken is stopped here and the stream has a start of its own to replay.
          const again = !interrupted ? undefined : interrupted.rerun ? await record({ type: "tool_rerun", callId: call.callId, toolName: call.name })
            : answer ? await record({ type: "tool_resumed", callId: call.callId, toolName: call.name }) : undefined;
          const context: ToolContext = { signal, context: options.context, timedOut: false,
            executionId: intent === undefined ? call.callId : `${hooks.sessionId}:${intent}` };
          const cursor = again ?? intent;
          yield event({
            type: "tool_execution_start",
            toolCallId: call.callId,
            toolName: call.name,
            arguments: structuredClone(call.arguments),
            executionId: context.executionId,
            ...(cursor === undefined ? {} : { cursor }),
            ...(interrupted?.rerun ? { rerun: true } : {}),
            ...(answer ? { resumed: true } : {}),
            // Settled without running: this start only pairs with the end that carries the error.
            ...(interrupted && !interrupted.rerun && !answer ? { interrupted: true } : {}),
          });
          let output: Content;
          let isError = false;
          let fatal: unknown;
          let paused: Pause | undefined;
          // False for a call that never ran: denied by a hook, or settled as interrupted. afterTool does not see those.
          let settled = !(interrupted && !interrupted.rerun && !answer);
          try {
            throwIfAborted(signal);
            if (answer && "error" in answer) throw new ToolError(answer.error);
            if (interrupted && !interrupted.rerun && !answer) throw new ToolError(INTERRUPTED);
            // An answered call is settled by its answer, whatever today's agent knows about the tool.
            if (!tool && !answer) throw new ToolError(`Unknown tool: ${call.name}`);
            if (finishing && call.name !== OUTPUT_TOOL) throw new ToolError("not run: the run finished");
            if (!answer) tool?.check(call.arguments);
            let arguments_ = structuredClone(call.arguments);
            // A paused call that has been answered is settled: its hooks do not get to decide again.
            for (const before of answer || !tool ? [] : [tool.beforeTool, this.beforeTool]) {
              const decision = before && await hooked(() => before({ ...call, arguments: structuredClone(arguments_) }, context), signal);
              throwIfAborted(signal);
              if (decision instanceof Pause) paused = decision;
              else if (decision && "deny" in decision) {
                settled = false;
                throw new ToolError(decision.deny);
              }
              else if (decision) arguments_ = structuredClone(decision.arguments);
              if (paused) break;
            }
            let value = paused ?? (answer || !tool ? answer?.result : await guarded(tool.execute(arguments_, signal, context), signal));
            throwIfAborted(signal);
            if (value instanceof Pause) paused = value;
            // A list of text and image parts stays a list; anything else that is not a string becomes JSON.
            const serialized = paused ? "" : typeof value === "string" ? value : JSON.stringify(value);
            if (serialized === undefined) throw new Error("tool returned no output");
            checkToolOutput(serialized);
            output = wellFormed(isContentParts(value) ? structuredClone(value) : serialized);
            if (call.name === OUTPUT_TOOL && options.outputSchema && !paused) accepted = { value: arguments_ };
          } catch (error) {
            // Only this run's own cancellation aborts it. An abort error a tool raised for its own reasons is a tool error.
            if (signal.aborted) {
              yield event({
                type: "tool_execution_end",
                toolCallId: call.callId,
                toolName: call.name,
                output: "run aborted",
                isError: true,
              });
              throw abortError();
            }
            isError = true;
            failures += 1;
            if (error instanceof RunError) fatal = error;
            // A ToolError is text written for the model; anything else is labelled with its kind.
            output = wellFormed(error instanceof ToolError ? error.message
              : `${error instanceof Error ? error.name : "Error"}: ${errorMessage(error)}`);
            if (Buffer.byteLength(String(output), "utf8") > MAX_TOOL_OUTPUT_BYTES)
              output = `ToolOutputLimitError: tool error exceeds ${MAX_TOOL_OUTPUT_BYTES} UTF-8 bytes`;
          }
          const after = this.afterTool;
          if (after && settled && !paused && !fatal) {
            try {
              const changed = await hooked(() => after(structuredClone(call), { output: structuredClone(output), isError }, context), signal);
              if (changed !== undefined) {
                const replaced = typeof changed === "string" ? { output: changed, isError } : changed;
                // A replaced result is held to the same rules as a tool's own.
                if (!isItem(replaced) || !(typeof replaced.output === "string" || isContentParts(replaced.output)) ||
                    Buffer.byteLength(JSON.stringify(replaced.output)) > MAX_TOOL_OUTPUT_BYTES)
                  throw new RunError("hook_error", "afterTool must return undefined, a string, or { output, isError } within the output limit");
                output = wellFormed(structuredClone(replaced.output));
                isError = replaced.isError === true;
              }
            } catch (error) {
              if (signal.aborted) {
                yield event({ type: "tool_execution_end", toolCallId: call.callId, toolName: call.name, output: "run aborted", isError: true });
                throw abortError();
              }
              fatal = error;
              output = errorMessage(error);
              isError = true;
            }
          }
          const observation = { role: "tool_result", callId: call.callId, toolName: call.name, output, isError };
          // The end event is owed to whoever saw the start, also when the journal refuses the result.
          let written: number | undefined;
          try {
            written = paused
              ? await record({ type: "tool_paused", callId: call.callId, toolName: call.name, payload: paused.payload })
              : await record({ type: "tool_result", item: observation, ...(accepted && call.name === OUTPUT_TOOL ? accepted : {}) });
          } catch (error) { fatal ??= error; }
          yield event({
            type: "tool_execution_end",
            toolCallId: call.callId,
            toolName: call.name,
            output,
            isError,
            ...(paused ? { paused: true } : {}),
            ...(written === undefined ? {} : { cursor: written }),
          });
          if (fatal) throw fatal;
          if (paused) {
            const pause: PendingPause = { toolCallId: call.callId, toolName: call.name, payload: paused.payload };
            terminal = true;
            yield event({ type: "agent_end", status: "paused", pause, usage: { ...usage } });
            return { text: "", output: [], history: transcript, usage: { ...usage }, pause };
          }
          transcript.push(observation);
        }
        errorTurns = failures === calls.length ? errorTurns + 1 : 0;
        calls = [];
        if (accepted) reply = { text: "", output: [], value: accepted.value };
        else if (errorTurns >= MAX_ERROR_TURNS)
          throw new RunError("repeated_tool_errors", `every tool call failed in ${MAX_ERROR_TURNS} model turns in a row`);
      }
    } catch (error) {
      if (terminal) throw error;
      if (signal.aborted) {
        yield event({ type: "agent_end", status: "aborted", usage: { ...usage } });
        throw abortError();
      }
      yield event({
        type: "agent_end",
        status: "failed",
        error: {
          code: failureCode(error),
          message: errorMessage(error),
          // True when the failure was transient (429, 5xx, a lost connection): trying again later may work.
          retryable: error instanceof ProviderError && error.retryable,
          ...(error instanceof RunError && error.limit ? { limit: error.limit } : {}),
        },
        usage: { ...usage },
      });
      throw error;
    } finally {
      controller.abort();
    }
  }

  /** One model request: its deltas, then the complete turn. Leaving it early releases the provider. */
  private async *request(request: ProviderRequest, release: () => void): AsyncGenerator<ProviderEvent, ProviderTurn> {
    const { signal } = request;
    const stream = this.provider.stream(request);
    let stopped = false;
    try {
      while (true) {
        const next = await guarded(stream.next(), signal);
        throwIfAborted(signal);
        if (next.done) {
          stopped = true;
          const turn: unknown = next.value;
          // A stream that just ends has not answered. Asking again would loop for ever.
          if (!isItem(turn) || !isItem(turn.assistant) || !Array.isArray(turn.toolCalls) || typeof turn.text !== "string")
            throw new ProviderError("the provider ended its stream without a complete turn");
          return wellFormed(next.value);
        }
        yield wellFormed(next.value);
      }
    } catch (error) {
      stopped = true;
      throw error;
    } finally {
      // Closing a stream must release its provider even when the consumer stops early.
      if (!stopped) release();
      await bounded(stream.return(undefined as never));
    }
  }

  async run(prompt: Content, history: Item[] = [], options: RunOptions = {}): Promise<RunResult> {
    const events = this.stream(prompt, history, options);
    try {
      while (true) {
        const next = await events.next();
        if (next.done) return next.value;
        await options.onEvent?.(next.value);
      }
    } finally {
      await events.return(undefined as never);
    }
  }
}

/** Our own failures have names; anything else that ends a run came from the provider. */
function failureCode(error: unknown): string {
  return error instanceof RunError || error instanceof FencedError || error instanceof StorageError ? error.code : "provider_error";
}

/** A hook that throws ends the run; it is not a tool failure the model can fix. */
async function hooked<T>(hook: () => T | Promise<T>, signal: AbortSignal): Promise<T> {
  try {
    return await guarded(hook(), signal);
  } catch (error) {
    if (signal.aborted) throw error;
    throw new RunError("hook_error", `tool hook failed: ${errorMessage(error)}`);
  }
}

/** Cleanup of host code is awaited, but not for ever, and its failure is not the run's. */
async function bounded(cleanup: Promise<unknown>): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  await Promise.race([cleanup.catch(() => {}), new Promise((resolve) => { timer = setTimeout(resolve, CANCEL_GRACE_MS); })]);
  clearTimeout(timer);
}

function checkToolOutput(output: string): void {
  if (Buffer.byteLength(output, "utf8") > MAX_TOOL_OUTPUT_BYTES)
    throw new ToolOutputLimitError(`tool output exceeds ${MAX_TOOL_OUTPUT_BYTES} UTF-8 bytes`);
}

function selfTest(): void {
  assert.equal(deniedReason("printf 'hello\\n'"), undefined);
  for (const command of [
    "rm -rf /",
    `r""m -rf /`,
    "sudo echo hi",
    "git reset --hard HEAD",
    "find . -delete",
    "mkfs.ext4 /dev/sda",
  ])
    assert.ok(deniedReason(command), command);
  console.log("self-test passed");
}

/** What the command line was given. The process's own values are read only where this file is the program. */
export interface CliOptions {
  /** Arguments after the program name. */
  argv: string[];
  env: Record<string, string | undefined>;
  /** Replaces the provider chosen by TINYAGENT_PROVIDER. */
  provider?: Provider;
  stdin?: Readable;
  stdout?: Writable;
  stderr?: Writable;
}

/** A command as it is shown to a person: control characters appear as escapes, so nothing can hide or rewrite the line. */
function visible(text: string): string {
  return text.replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g,
    (character) => `\\x${character.charCodeAt(0).toString(16).padStart(2, "0")}`);
}

const VALUE_FLAGS = ["--prompt", "--max-steps", "--max-seconds", "--max-tokens", "--cwd", "--allow", "--resume", "--mode"];

/** The coding CLI as a function. It resolves to the exit code. */
export async function runCli(options: CliOptions): Promise<number> {
  const { env } = options;
  const stdout = options.stdout ?? process.stdout;
  const stderr = options.stderr ?? process.stderr;
  const flags = new Map<string, string[]>();
  const positional: string[] = [];
  for (let index = 0; index < options.argv.length; index++) {
    const name = options.argv[index] === "-p" ? "--prompt" : options.argv[index];
    if (!name.startsWith("--")) positional.push(name);
    else if (!VALUE_FLAGS.includes(name)) flags.set(name, []);
    else if (++index >= options.argv.length) throw new Error(`${name} requires a value`);
    else flags.set(name, [...(flags.get(name) ?? []), options.argv[index]]);
  }
  const flag = (name: string) => flags.get(name)?.at(-1);
  if (flags.has("--self-test")) {
    selfTest();
    return 0;
  }
  const directory = env.TINYAGENT_SESSION_DIR ?? ".tinyagent/sessions";
  if (flags.has("--list-sessions")) {
    stdout.write(`${new FileStorage(directory).list().join("\n")}\n`);
    return 0;
  }
  const limit = (name: string, variable: string): number | undefined => {
    const text = flag(name) ?? env[variable];
    if (text === undefined) return undefined;
    if (!text.trim() || !(Number(text) >= 0)) throw new Error(`${name} must be a nonnegative number`);
    return Number(text);
  };
  const sessionId = flag("--resume");
  const budget: ContextBudget | undefined = env.TINYAGENT_CONTEXT_TOKENS ? {
    maxTokens: Number(env.TINYAGENT_CONTEXT_TOKENS),
    responseReserve: Number(env.TINYAGENT_RESPONSE_RESERVE ?? 4096),
    countTokens: (request) => Buffer.byteLength(JSON.stringify(request)) + 1024,
  } : undefined;
  const providerName = (env.TINYAGENT_PROVIDER ?? "openai").toLowerCase();
  let provider: Provider;
  let model: string;
  if (providerName === "anthropic") {
    if (!options.provider && !env.ANTHROPIC_API_KEY) throw new Error("ANTHROPIC_API_KEY is not set");
    provider = options.provider ?? new AnthropicProvider({ apiKey: env.ANTHROPIC_API_KEY, baseUrl: env.TINYAGENT_BASE_URL });
    model = env.TINYAGENT_MODEL ?? "claude-sonnet-4-6";
  } else if (providerName === "openai") {
    if (!options.provider && !env.OPENAI_API_KEY) throw new Error("OPENAI_API_KEY is not set");
    provider = options.provider ?? new OpenAIProvider({ apiKey: env.OPENAI_API_KEY, baseUrl: env.TINYAGENT_BASE_URL });
    model = env.TINYAGENT_MODEL ?? "gpt-5.6";
  } else if (providerName === "chat") {
    // Many OpenAI-compatible servers need no key, but they do need to be told which model.
    if (!env.TINYAGENT_MODEL) throw new Error("TINYAGENT_MODEL is not set: TINYAGENT_PROVIDER=chat has no default model");
    provider = options.provider ??
      new ChatCompletionsProvider({ apiKey: env.OPENAI_API_KEY ?? "", baseUrl: env.TINYAGENT_BASE_URL });
    model = env.TINYAGENT_MODEL ?? "";
  } else {
    throw new Error(`unsupported TINYAGENT_PROVIDER: ${providerName}`);
  }

  const prompt = flag("--prompt");
  const rpc = flags.has("--mode");
  if (rpc && flag("--mode") !== "rpc") throw new Error("supported mode: rpc");
  const lines = prompt === undefined && !rpc && positional[0] !== "serve"
    ? createInterface({ input: options.stdin ?? process.stdin, output: stdout }) : undefined;
  const typed = lines?.[Symbol.asyncIterator]();
  /** The next line the user types, or undefined at the end of input. */
  const readLine = async (): Promise<string | undefined> => {
    const next = await typed?.next();
    return next && !next.done ? next.value : undefined;
  };
  let yolo = flags.has("--yolo") || env.TINYAGENT_YOLO === "1";
  // An allow pattern must match the whole command, and never auto-approves a command that
  // chains, substitutes or redirects: "echo .*" must not approve "echo hi; curl x | sh".
  const allowed = (flags.get("--allow") ?? []).map((pattern) => new RegExp(`^(?:${pattern})$`));
  const autoApproved = (command: string): boolean =>
    !/[;&|`$<>()\\\n\r]/.test(command) && allowed.some((pattern) => pattern.test(command));
  // RPC and HTTP clients cannot be asked, so those modes rely on the blacklist alone.
  const approve: Approve | undefined = rpc || positional[0] === "serve" ? undefined : async (command) => {
    if (yolo || autoApproved(command)) return true;
    if (!lines) return "one-shot mode runs a command only with --yolo or a matching --allow";
    stderr.write(`\n$ ${visible(command)}\nRun it? y: yes, n: no, a: yes to all from now on, or type why not: `);
    const answer = (await readLine())?.trim() ?? "n";
    const word = answer.toLowerCase();
    if (word === "a" || word === "all") yolo = true;
    return yolo || word === "y" || word === "yes" ? true : word === "n" || word === "no" || !word ? false : answer;
  };
  const bash = bashTool({ cwd: flag("--cwd"), timeoutSeconds: 120, approve });
  const agent = new Agent({ model, instructions: INSTRUCTIONS, tools: [bash], provider,
    maxSteps: limit("--max-steps", "TINYAGENT_MAX_STEPS"), maxSeconds: limit("--max-seconds", "TINYAGENT_MAX_SECONDS"),
    maxTokens: limit("--max-tokens", "TINYAGENT_MAX_TOKENS") });
  if (positional[0] === "serve") {
    const server = await serve(agent, {
      host: env.TINYAGENT_HOST,
      port: env.TINYAGENT_PORT === undefined ? undefined : Number(env.TINYAGENT_PORT),
      token: env.TINYAGENT_TOKEN,
      allowRemote: env.TINYAGENT_ALLOW_REMOTE === "1",
      allowedHosts: env.TINYAGENT_ALLOWED_HOSTS?.split(",").map((name) => name.trim()).filter(Boolean),
      allowedOrigins: env.TINYAGENT_ALLOWED_ORIGINS?.split(",").map((origin) => origin.trim()).filter(Boolean),
      directory,
      budget,
    });
    stderr.write(`TinyAgent listening at ${server.url}\n`);
    const shutdown = () => {
      void server.close().finally(() => {
        process.off("SIGINT", shutdown);
        process.off("SIGTERM", shutdown);
      });
    };
    process.on("SIGINT", shutdown);
    process.on("SIGTERM", shutdown);
    return 0;
  }
  if (rpc) return runRpcProcess(agent, { directory, sessionId, resume: sessionId !== undefined, budget });

  const session = new Session(agent, { directory, sessionId, resume: sessionId !== undefined, budget });
  const show = (event: AgentEvent) => {
    if (event.type === "text_delta") stdout.write(event.delta);
    else if (event.type === "tool_execution_start") stderr.write(`\n$ ${visible(String(event.arguments.command))}\n`);
    else if (event.type === "tool_execution_end")
      stderr.write(`${typeof event.output === "string" ? event.output : JSON.stringify(event.output)}\n`);
  };
  let active: AbortController | undefined;
  let exiting = false;
  const interrupt = () => {
    exiting = true;
    active?.abort();
    lines?.close();
  };
  process.on("SIGINT", interrupt);
  try {
    if (prompt !== undefined) {
      // One-shot: a single run, then an exit code that says whether it completed.
      active = new AbortController();
      try {
        // A run that --resume found interrupted is finished first.
        if (session.resumable && !session.paused) {
          await session.resume({ signal: active.signal, onEvent: show });
          stdout.write("\n");
        }
        await session.run(prompt, { signal: active.signal, onEvent: show });
        stdout.write("\n");
        return 0;
      } catch (error) {
        stderr.write(`error: ${errorMessage(error)}\n`);
        return 1;
      }
    }
    stdout.write(`TinyAgent session ${session.id}. Type a task, or Ctrl-C or Ctrl-D to exit.\n`);
    if (session.paused) stdout.write(`This session's run is paused: ${visible(JSON.stringify(session.paused.payload))}\n` +
      "Type /resume <answer> to continue it, or /abandon to drop it.\n");
    else if (session.resumable) {
      stdout.write("Continuing the interrupted run. Type /abandon afterwards if it fails again.\n");
      active = new AbortController();
      try { await session.resume({ signal: active.signal, onEvent: show }); }
      catch (error) { if (!active.signal.aborted) stderr.write(`error: ${errorMessage(error)}\n`); }
      finally { active = undefined; }
      stdout.write("\n");
    }
    while (!exiting) {
      stdout.write("\n> ");
      const line = await readLine();
      if (line === undefined) break;
      const task = line.trim();
      if (!task) continue;
      active = new AbortController();
      try {
        if (task === "/history") stdout.write(`${JSON.stringify(session.records, null, 2)}\n`);
        else if (task.startsWith("/reconcile ")) await session.reconcile(task.slice("/reconcile ".length));
        else if (task === "/compact") await session.compact({ signal: active.signal });
        else if (task === "/resume") await session.resume({ signal: active.signal, onEvent: show });
        // The text after /resume answers the question a paused run is waiting on.
        else if (task.startsWith("/resume ")) await session.resume({ signal: active.signal, onEvent: show,
          result: task.slice("/resume ".length).trim() });
        else if (task === "/abandon") await session.abandon();
        else await session.run(task, { signal: active.signal, onEvent: show });
        stdout.write("\n");
      } catch (error) {
        if (!active.signal.aborted) stderr.write(`error: ${errorMessage(error)}\n`);
      } finally {
        active = undefined;
      }
    }
    stdout.write("\n");
    return 0;
  } finally {
    process.off("SIGINT", interrupt);
    lines?.close();
    await session.close();
  }
}
