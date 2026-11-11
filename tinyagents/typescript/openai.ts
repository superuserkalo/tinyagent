import {
  ProviderError,
  isItem,
  isContentParts,
  imageUrl,
  errorMessage,
  abortError,
  throwIfAborted,
  utf8Length,
  environment,
  type Item,
  type EventTransport,
  type Provider,
  type ProviderRequest,
  type ProviderEvent,
  type ProviderTurn,
  type ToolCall,
  type Transport,
} from "./types.ts";
import {
  Arguments,
  MAX_SSE_BYTES,
  httpSseEvents,
  indexValue,
  objectValue,
  objectArray,
  parseObject,
  stringValue,
  type HttpRequestOptions,
} from "./sse.ts";

const OPENAI_API = "https://api.openai.com/v1/responses";

export interface OpenAIProviderOptions extends HttpRequestOptions {
  apiKey?: string;
  eventTransport?: EventTransport;
}

export class OpenAIProvider implements Provider {
  private readonly options: OpenAIProviderOptions;
  private readonly eventTransport: EventTransport;

  constructor(options: OpenAIProviderOptions = {}) {
    this.options = { ...options };
    this.eventTransport = options.eventTransport ?? this.httpEvents.bind(this);
  }

  async *stream(request: ProviderRequest): AsyncGenerator<ProviderEvent, ProviderTurn> {
    throwIfAborted(request.signal);
    const payload: Item = {
      model: request.model,
      instructions: request.instructions,
      input: openAIInput(request.history),
      tools: request.tools.map((tool) => tool.definition()),
      parallel_tool_calls: false,
      store: false,
      include: ["reasoning.encrypted_content"],
      stream: true,
      ...this.options.extra,
      ...request.extra,
    };
    const output = new Map<number, Item>();
    const argumentsByIndex = new Map<number, Arguments>();
    let terminal = false;
    let usage: ProviderTurn["usage"];
    try {
      for await (const event of this.eventTransport(payload, request.signal)) {
        throwIfAborted(request.signal);
        const type = stringValue(event, "type", true);
        if (type === "response.output_item.added" || type === "response.output_item.done") {
          const index = indexValue(event.output_index);
          const item = structuredClone(objectValue(event.item, "output item"));
          if (stringValue(item, "type", true) === "function_call") {
            const raw = stringValue(item, "arguments");
            if (raw) {
              const arguments_ = new Arguments();
              arguments_.set(raw);
              argumentsByIndex.set(index, arguments_);
            }
          }
          output.set(index, item);
        } else if (type === "response.reasoning_summary_text.delta" || type === "response.reasoning_text.delta") {
          const delta = stringValue(event, "delta");
          if (delta) yield { type: "reasoning_delta", delta };
        } else if (type === "response.output_text.delta") {
          const delta = stringValue(event, "delta");
          if (delta) yield { type: "text_delta", delta };
        } else if (
          type === "response.function_call_arguments.delta" ||
          type === "response.function_call_arguments.done"
        ) {
          const index = indexValue(event.output_index);
          let arguments_ = argumentsByIndex.get(index);
          if (!arguments_) {
            arguments_ = new Arguments();
            argumentsByIndex.set(index, arguments_);
          }
          if (type.endsWith(".delta")) arguments_.append(stringValue(event, "delta"));
          else arguments_.set(stringValue(event, "arguments"));
        } else if (type === "response.completed") {
          const response = objectValue(event.response, "completed response");
          if ("status" in response && response.status !== "completed")
            throw new ProviderError("OpenAI response is incomplete");
          if ("output" in response) {
            output.clear();
            for (const [index, item] of objectArray(response.output, "output").entries())
              output.set(index, structuredClone(item));
          }
          if (isItem(response.usage) && typeof response.usage.input_tokens === "number" &&
              typeof response.usage.output_tokens === "number")
            usage = { inputTokens: response.usage.input_tokens, outputTokens: response.usage.output_tokens };
          terminal = true;
          break;
        } else if (type === "response.failed" || type === "response.incomplete" || type === "error") {
          throw new ProviderError(providerErrorMessage(event));
        }
      }
    } catch (error) {
      if (request.signal.aborted) throw abortError();
      if (error instanceof ProviderError) throw error;
      throw new ProviderError(errorMessage(error));
    }
    throwIfAborted(request.signal);
    if (!terminal) throw new ProviderError("OpenAI stream ended before response.completed");
    for (const [index, item] of output) {
      if (item.type === "function_call" && !item.arguments && argumentsByIndex.has(index))
        item.arguments = argumentsByIndex.get(index)?.value();
    }
    const turn = openAITurn([...output.entries()].sort(([a], [b]) => a - b).map(([, item]) => item));
    return usage ? { ...turn, usage } : turn;
  }

  private async *httpEvents(payload: Item, signal: AbortSignal): AsyncGenerator<Item> {
    const key = this.options.apiKey ?? environment("OPENAI_API_KEY");
    if (!key && !Object.keys(this.options.headers ?? {}).some((name) => name.toLowerCase() === "authorization"))
      throw new ProviderError("OPENAI_API_KEY is not set");
    yield* httpSseEvents(
      this.options.endpoint ?? (this.options.baseUrl ? this.options.baseUrl.replace(/\/+$/, "") + "/responses" : OPENAI_API),
      payload,
      { Authorization: "Bearer " + key, "Content-Type": "application/json", Accept: "text/event-stream" },
      signal,
      this.options,
    );
  }
}

export class CompleteTransportProvider implements Provider {
  private readonly transport: Transport;

  constructor(transport: Transport) {
    this.transport = transport;
  }

  async *stream(request: ProviderRequest): AsyncGenerator<ProviderEvent, ProviderTurn> {
    throwIfAborted(request.signal);
    const output = await this.transport({
      model: request.model,
      instructions: request.instructions,
      input: openAIInput(request.history),
      tools: request.tools.map((tool) => tool.definition()),
      parallel_tool_calls: false,
      store: false,
      include: ["reasoning.encrypted_content"],
    });
    throwIfAborted(request.signal);
    const turn = openAITurn(structuredClone(output));
    if (turn.text) yield { type: "text_delta", delta: turn.text };
    return turn;
  }
}

function openAITurn(output: Item[]): ProviderTurn {
  const toolCalls: ToolCall[] = [];
  const ids = new Set<string>();
  const text: string[] = [];
  for (const item of output) {
    const type = stringValue(item, "type", true);
    if (type === "function_call") {
      const name = stringValue(item, "name", true);
      const callId = stringValue(item, "call_id", true);
      const raw = stringValue(item, "arguments");
      if (ids.has(callId)) throw new ProviderError("duplicate OpenAI tool call");
      ids.add(callId);
      if (utf8Length(raw) > MAX_SSE_BYTES)
        throw new ProviderError("tool arguments exceed their byte limit");
      let arguments_: Item;
      try {
        arguments_ = parseObject(raw);
      } catch (error) {
        throw new ProviderError("invalid arguments for tool " + name + ": " + errorMessage(error));
      }
      toolCalls.push({ callId, name, arguments: arguments_ });
    } else if (type === "message") {
      for (const part of objectArray(item.content, "message content")) {
        if (stringValue(part, "type", true) === "output_text") text.push(stringValue(part, "text"));
      }
    }
  }
  const answer = text.join("");
  return { text: answer, assistant: assistantMessage(output, toolCalls, answer), output, toolCalls };
}

function openAIInput(history: Item[]): Item[] {
  const providerInput: Item[] = [];
  for (const entry of history) {
    if (entry.role === "user") {
      providerInput.push({ role: "user", content: openAIContent(entry.content ?? "") });
    } else if (entry.role === "assistant") {
      const metadata = isItem(entry.providerMetadata) ? entry.providerMetadata : undefined;
      if (metadata?.provider === "openai-responses" && Array.isArray(metadata.output)) {
        providerInput.push(...metadata.output.filter(isItem));
        continue;
      }
      if (!Array.isArray(entry.content)) continue;
      for (const block of entry.content) {
        if (!isItem(block)) continue;
        if (block.type === "text" && typeof block.text === "string") {
          providerInput.push({ role: "assistant", content: block.text });
        } else if (block.type === "tool_call") {
          providerInput.push({
            type: "function_call",
            call_id: block.callId,
            name: block.name,
            arguments: JSON.stringify(block.arguments ?? {}),
          });
        }
      }
    } else if (entry.role === "tool_result") {
      providerInput.push({
        type: "function_call_output",
        call_id: entry.callId,
        output: openAIContent(entry.output ?? ""),
      });
    } else {
      // Accept pre-0.0.1 OpenAI-native history for migration.
      providerInput.push(entry);
    }
  }
  return providerInput;
}

/** Text stays a string; a list of parts becomes Responses input parts. */
function openAIContent(content: unknown): unknown {
  if (!isContentParts(content)) return content;
  return content.map((part) => part.type === "text" ? { type: "input_text", text: part.text }
    : { type: "input_image", image_url: imageUrl(part) });
}

function assistantMessage(output: Item[], calls: ToolCall[], text: string): Item {
  const callsById = new Map(calls.map((call) => [call.callId, call]));
  const content: Item[] = [];
  for (const item of output) {
    if (item.type === "reasoning") {
      const summary = Array.isArray(item.summary)
        ? item.summary
            .filter(isItem)
            .map((part) => (typeof part.text === "string" ? part.text : ""))
            .join("\n\n")
        : "";
      content.push({ type: "reasoning", ...(summary ? { text: summary } : {}) });
    } else if (item.type === "message" && Array.isArray(item.content)) {
      for (const part of item.content) {
        if (isItem(part) && part.type === "output_text" && typeof part.text === "string")
          content.push({ type: "text", text: part.text });
      }
    } else if (item.type === "function_call" && typeof item.call_id === "string") {
      const call = callsById.get(item.call_id);
      if (call) content.push({ type: "tool_call", callId: call.callId, name: call.name, arguments: call.arguments });
    }
  }
  if (text && !content.some((block) => block.type === "text")) content.push({ type: "text", text });
  return {
    role: "assistant",
    content,
    providerMetadata: { provider: "openai-responses", output },
  };
}

function providerErrorMessage(event: Item): string {
  if (event.type === "error") return typeof event.message === "string" ? event.message : "OpenAI stream error";
  if (isItem(event.response)) {
    if (isItem(event.response.error))
      return String(event.response.error.message ?? event.response.error.code ?? "OpenAI response failed");
    if (isItem(event.response.incomplete_details) && event.response.incomplete_details.reason)
      return `OpenAI response incomplete: ${event.response.incomplete_details.reason}`;
  }
  return `OpenAI stream failed: ${event.type}`;
}
