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
  randomId,
  sleep,
  utf8Length,
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
import { FencedError } from "./storage.ts";
import { MAX_TOOL_OUTPUT_BYTES } from "./protocol.ts";

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
    this.newId = options.newId ?? randomId;
    this.sleep = options.sleep ?? sleep;
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
    const host = hooks.host ?? ((call) => call());

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
              const decision = before && await hooked(() => host(() => before({ ...call, arguments: structuredClone(arguments_) }, context)), signal);
              throwIfAborted(signal);
              if (decision instanceof Pause) paused = decision;
              else if (decision && "deny" in decision) {
                settled = false;
                throw new ToolError(decision.deny);
              }
              else if (decision) arguments_ = structuredClone(decision.arguments);
              if (paused) break;
            }
            let value = paused ?? (answer || !tool ? answer?.result : await guarded(host(() => tool.execute(arguments_, signal, context)), signal));
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
            if (utf8Length(String(output)) > MAX_TOOL_OUTPUT_BYTES)
              output = `ToolOutputLimitError: tool error exceeds ${MAX_TOOL_OUTPUT_BYTES} UTF-8 bytes`;
          }
          const after = this.afterTool;
          if (after && settled && !paused && !fatal) {
            try {
              const changed = await hooked(() => host(() => after(structuredClone(call), { output: structuredClone(output), isError }, context)), signal);
              if (changed !== undefined) {
                const replaced = typeof changed === "string" ? { output: changed, isError } : changed;
                // A replaced result is held to the same rules as a tool's own.
                if (!isItem(replaced) || !(typeof replaced.output === "string" || isContentParts(replaced.output)) ||
                    utf8Length(JSON.stringify(replaced.output)) > MAX_TOOL_OUTPUT_BYTES)
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
  if (utf8Length(output) > MAX_TOOL_OUTPUT_BYTES)
    throw new ToolOutputLimitError(`tool output exceeds ${MAX_TOOL_OUTPUT_BYTES} UTF-8 bytes`);
}
