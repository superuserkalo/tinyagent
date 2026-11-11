import { ProviderError, throwIfAborted, type Item, type Provider, type ProviderEvent, type ProviderRequest,
  type ProviderTurn, type ToolCall } from "./types.ts";

/**
 * One model turn of a script: text for a final answer, tool calls, or an
 * object for the details. `deltas` are streamed before the turn completes
 * (default: the text in one piece). `error` is thrown instead of answering.
 * A function is called with the request and returns any of these.
 */
export type ScriptedTurn =
  | string
  | ToolCall[]
  | { text?: string; toolCalls?: ToolCall[]; deltas?: string[]; usage?: ProviderTurn["usage"]; error?: Error }
  | ((request: ProviderRequest) => ScriptedTurn | Promise<ScriptedTurn>);

/**
 * A provider for tests: it plays a fixed list of turns, one per model request,
 * and keeps the requests it was sent. No network, no keys.
 */
export class ScriptedProvider implements Provider {
  /** Every request received, in order, with its history as it was at that moment. */
  readonly requests: ProviderRequest[] = [];
  private readonly turns: ScriptedTurn[];

  constructor(turns: ScriptedTurn[]) {
    this.turns = [...turns];
  }

  /** Turns not yet played. A finished script has none. */
  get remaining(): number {
    return this.turns.length;
  }

  async *stream(request: ProviderRequest): AsyncGenerator<ProviderEvent, ProviderTurn> {
    throwIfAborted(request.signal);
    this.requests.push({ ...request, history: structuredClone(request.history) });
    let next = this.turns.shift();
    if (next === undefined) throw new ProviderError(`ScriptedProvider has no turn for request ${this.requests.length}`);
    while (typeof next === "function") next = await next(request);
    const turn = typeof next === "string" ? { text: next } : Array.isArray(next) ? { toolCalls: next } : next;
    if (turn.error) throw turn.error;
    const text = turn.text ?? "";
    for (const delta of turn.deltas ?? (text ? [text] : [])) {
      throwIfAborted(request.signal);
      yield { type: "text_delta", delta };
    }
    throwIfAborted(request.signal);
    const toolCalls = structuredClone(turn.toolCalls ?? []);
    const content: Item[] = [...(text ? [{ type: "text", text }] : []),
      ...toolCalls.map((call) => ({ type: "tool_call", ...call }))];
    return { text, assistant: { role: "assistant", content }, output: [], toolCalls,
      ...(turn.usage ? { usage: turn.usage } : {}) };
  }
}
