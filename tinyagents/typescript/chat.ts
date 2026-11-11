import { ProviderError, isItem, isContentParts, imageUrl, errorMessage, abortError, throwIfAborted, assistantText, environment, type Item, type EventTransport,
  type Provider, type ProviderRequest, type ProviderEvent, type ProviderTurn, type ToolCall } from "./types.ts";
import { Arguments, httpSseEvents, indexValue, objectArray, parseObject, type HttpRequestOptions } from "./sse.ts";

const OPENAI_BASE = "https://api.openai.com/v1";
/** What the transport yields for the stream's closing `data: [DONE]`. */
const DONE = { done: true };

export interface ChatCompletionsProviderOptions extends HttpRequestOptions {
  /** Default: OPENAI_API_KEY, read when a request starts. Without either, no Authorization header is sent. */
  apiKey?: string;
  eventTransport?: EventTransport;
}

/**
 * The OpenAI-compatible `/chat/completions` streaming API, which most gateways
 * and self-hosted model servers offer. Set `baseUrl` to the server's API root.
 */
export class ChatCompletionsProvider implements Provider {
  private readonly options: ChatCompletionsProviderOptions;
  private readonly eventTransport: EventTransport;

  constructor(options: ChatCompletionsProviderOptions = {}) {
    this.options = { ...options };
    this.eventTransport = options.eventTransport ?? this.httpEvents.bind(this);
  }

  async *stream(request: ProviderRequest): AsyncGenerator<ProviderEvent, ProviderTurn> {
    throwIfAborted(request.signal);
    const tools = request.tools.map((tool) => ({ type: "function",
      function: { name: tool.name, description: tool.description, parameters: tool.parameters } }));
    const payload: Item = {
      model: request.model,
      messages: [...(request.instructions ? [{ role: "system", content: request.instructions }] : []),
        ...chatMessages(request.history)],
      ...(tools.length ? { tools } : {}),
      stream: true,
      stream_options: { include_usage: true },
      ...this.options.extra,
      ...request.extra,
    };
    const text: string[] = [];
    // A tool call arrives in pieces: its ID and name once, its arguments as string fragments.
    const calls = new Map<number, { id: string; name: string; arguments: Arguments }>();
    let finished: string | undefined;
    let usage: ProviderTurn["usage"];
    try {
      for await (const event of this.eventTransport(payload, request.signal)) {
        throwIfAborted(request.signal);
        if (event.done === true) break;
        if (isItem(event.error)) throw new ProviderError(String(event.error.message ?? "Chat Completions stream error"));
        // Some servers omit usage; others send it in a last chunk that has no choices.
        if (isItem(event.usage) && typeof event.usage.prompt_tokens === "number" &&
            typeof event.usage.completion_tokens === "number")
          usage = { inputTokens: event.usage.prompt_tokens, outputTokens: event.usage.completion_tokens };
        const choice = Array.isArray(event.choices) ? objectArray(event.choices, "choices")[0] : undefined;
        if (!choice) continue;
        const delta = isItem(choice.delta) ? choice.delta : {};
        const reasoning = typeof delta.reasoning_content === "string" ? delta.reasoning_content : delta.reasoning;
        if (typeof reasoning === "string" && reasoning) yield { type: "reasoning_delta", delta: reasoning };
        if (typeof delta.content === "string" && delta.content) {
          text.push(delta.content);
          yield { type: "text_delta", delta: delta.content };
        }
        for (const part of Array.isArray(delta.tool_calls) ? objectArray(delta.tool_calls, "tool calls") : []) {
          const index = indexValue(part.index ?? 0);
          const call = calls.get(index) ?? { id: "", name: "", arguments: new Arguments() };
          calls.set(index, call);
          const fn = isItem(part.function) ? part.function : {};
          if (typeof part.id === "string" && part.id) call.id = part.id;
          if (typeof fn.name === "string" && fn.name) call.name = fn.name;
          if (typeof fn.arguments === "string") call.arguments.append(fn.arguments);
        }
        if (typeof choice.finish_reason === "string") finished = choice.finish_reason;
      }
    } catch (error) {
      if (request.signal.aborted) throw abortError();
      if (error instanceof ProviderError) throw error;
      throw new ProviderError(errorMessage(error));
    }
    throwIfAborted(request.signal);
    if (!finished) throw new ProviderError("Chat Completions stream ended before a finish_reason");
    if (!["stop", "tool_calls", "function_call"].includes(finished))
      throw new ProviderError("Chat Completions response is incomplete: " + finished);
    const toolCalls: ToolCall[] = [];
    for (const [, call] of [...calls.entries()].sort(([a], [b]) => a - b)) {
      if (!call.id || !call.name || toolCalls.some((other) => other.callId === call.id))
        throw new ProviderError("invalid Chat Completions tool call");
      try {
        toolCalls.push({ callId: call.id, name: call.name, arguments: parseObject(call.arguments.value() || "{}") });
      } catch (error) {
        throw new ProviderError("invalid arguments for tool " + call.name + ": " + errorMessage(error));
      }
    }
    const answer = text.join("");
    const content: Item[] = [...(answer ? [{ type: "text", text: answer }] : []),
      ...toolCalls.map((call) => ({ type: "tool_call", ...call }))];
    return { text: answer, assistant: { role: "assistant", content }, output: [], toolCalls, ...(usage ? { usage } : {}) };
  }

  private async *httpEvents(payload: Item, signal: AbortSignal): AsyncGenerator<Item> {
    // Many compatible servers take no key. An empty apiKey means: send none.
    const key = this.options.apiKey ?? environment("OPENAI_API_KEY");
    yield* httpSseEvents(
      this.options.endpoint ?? (this.options.baseUrl ?? OPENAI_BASE).replace(/\/+$/, "") + "/chat/completions",
      payload,
      { ...(key ? { Authorization: "Bearer " + key } : {}), "Content-Type": "application/json", Accept: "text/event-stream" },
      signal,
      this.options,
      DONE,
    );
  }
}

/** Text stays a string; a list of parts becomes Chat Completions content parts. */
function chatContent(content: unknown): unknown {
  if (!isContentParts(content)) return content;
  return content.map((part) => part.type === "text" ? { type: "text", text: part.text }
    : { type: "image_url", image_url: { url: imageUrl(part) } });
}

/** Provider-neutral history as Chat Completions messages. */
function chatMessages(history: Item[]): Item[] {
  const messages: Item[] = [];
  let waiting: Item[] = [];
  const flush = () => {
    if (waiting.length) messages.push({ role: "user", content: chatContent(waiting) });
    waiting = [];
  };
  for (const entry of history) {
    if (entry.role === "tool_result") {
      const parts = isContentParts(entry.output) ? entry.output : undefined;
      const images = parts?.filter((part) => part.type === "image") ?? [];
      const words = parts ? parts.map((part) => part.type === "text" ? part.text : "").join("") || "(see the image)"
        : entry.output ?? "";
      messages.push({ role: "tool", tool_call_id: entry.callId, content: words });
      // A tool message carries text only, and the tool messages of one turn must stay together,
      // so the images of a turn's results follow all of them in one user message.
      waiting.push(...images);
      continue;
    }
    flush();
    if (entry.role === "user") messages.push({ role: "user", content: chatContent(entry.content ?? "") });
    else if (entry.role === "assistant") {
      const calls = (Array.isArray(entry.content) ? entry.content : []).filter((block) => isItem(block) && block.type === "tool_call")
        .map((block: Item) => ({ id: block.callId, type: "function",
          function: { name: block.name, arguments: JSON.stringify(block.arguments ?? {}) } }));
      // A turn with neither text nor calls says nothing, and the API rejects it.
      if (assistantText(entry) || calls.length)
        messages.push({ role: "assistant", content: assistantText(entry) || null, ...(calls.length ? { tool_calls: calls } : {}) });
    } else messages.push(entry);
  }
  flush();
  return messages;
}
