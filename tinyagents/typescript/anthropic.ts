import {
  ProviderError,
  isItem,
  isContentParts,
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
} from "./types.ts";
import {
  Arguments,
  MAX_SSE_BYTES,
  httpSseEvents,
  indexValue,
  objectValue,
  parseObject,
  stringValue,
  type HttpRequestOptions,
} from "./sse.ts";

const ANTHROPIC_API = "https://api.anthropic.com/v1/messages";

export interface AnthropicProviderOptions extends HttpRequestOptions {
  apiKey?: string;
  eventTransport?: EventTransport;
  maxTokens?: number;
}

interface ContentBlock {
  value: Item;
  arguments: Arguments;
  open: boolean;
}

export class AnthropicProvider implements Provider {
  private readonly options: AnthropicProviderOptions;
  private readonly eventTransport: EventTransport;
  private readonly maxTokens: number;

  constructor(options: AnthropicProviderOptions = {}) {
    this.options = { ...options };
    this.maxTokens = options.maxTokens ?? 8192;
    if (!Number.isSafeInteger(this.maxTokens) || this.maxTokens < 1)
      throw new Error("maxTokens must be a positive integer");
    this.eventTransport = options.eventTransport ?? this.httpEvents.bind(this);
  }

  async *stream(request: ProviderRequest): AsyncGenerator<ProviderEvent, ProviderTurn> {
    throwIfAborted(request.signal);
    const tools = request.tools.map((tool) => ({
      name: tool.name,
      description: tool.description,
      input_schema: tool.parameters,
    }));
    const payload: Item = {
      model: request.model,
      messages: anthropicInput(request.history),
      max_tokens: this.maxTokens,
      stream: true,
      ...(request.instructions ? { system: request.instructions } : {}),
      ...(tools.length ? { tools } : {}),
      ...this.options.extra,
      ...request.extra,
    };
    const blocks = new Map<number, ContentBlock>();
    let message: Item | undefined;
    let usage: Item = {};
    let terminal = false;
    try {
      for await (const event of this.eventTransport(payload, request.signal)) {
        throwIfAborted(request.signal);
        const type = stringValue(event, "type", true);
        if (type === "message_start") {
          if (message) throw new ProviderError("repeated Anthropic message_start");
          message = structuredClone(objectValue(event.message, "message"));
          if ("usage" in message) usage = { ...objectValue(message.usage, "usage") };
        } else if (type === "content_block_start") {
          const index = indexValue(event.index);
          if (!message || blocks.has(index)) throw new ProviderError("invalid Anthropic block order");
          const value = structuredClone(objectValue(event.content_block, "content block"));
          stringValue(value, "type", true);
          blocks.set(index, { value, arguments: new Arguments(), open: true });
        } else if (type === "content_block_delta" || type === "content_block_stop") {
          const block = blocks.get(indexValue(event.index));
          if (!block?.open) throw new ProviderError("Anthropic event referenced an unopened block");
          if (type === "content_block_stop") {
            if (block.value.type === "tool_use") {
              const raw = block.arguments.value();
              if (raw) {
                try {
                  block.value.input = parseObject(raw);
                } catch (error) {
                  throw new ProviderError(
                    "invalid arguments for tool " + block.value.name + ": " + errorMessage(error),
                  );
                }
              }
              objectValue(block.value.input, "tool arguments");
            }
            block.open = false;
            continue;
          }
          const delta = objectValue(event.delta, "block delta");
          const kind = stringValue(delta, "type", true);
          let expected: string;
          let field: string;
          switch (kind) {
            case "text_delta":
              expected = "text";
              field = "text";
              break;
            case "thinking_delta":
              expected = "thinking";
              field = "thinking";
              break;
            case "signature_delta":
              expected = "thinking";
              field = "signature";
              break;
            case "input_json_delta":
              expected = "tool_use";
              field = "partial_json";
              break;
            default:
              continue;
          }
          if (block.value.type !== expected) throw new ProviderError("Anthropic delta does not match its block");
          const value = stringValue(delta, field);
          if (kind === "input_json_delta") {
            block.arguments.append(value);
          } else {
            const previous = field in block.value ? stringValue(block.value, field) : "";
            block.value[field] = previous + value;
            if (kind === "text_delta" && value) yield { type: "text_delta", delta: value };
            if (kind === "thinking_delta" && value) yield { type: "reasoning_delta", delta: value };
          }
        } else if (type === "message_delta") {
          if (!message) throw new ProviderError("Anthropic delta preceded message_start");
          message = { ...message, ...structuredClone(objectValue(event.delta, "message delta")) };
          if ("usage" in event) usage = { ...usage, ...objectValue(event.usage, "usage delta") };
        } else if (type === "message_stop") {
          terminal = true;
          break;
        } else if (type === "error") {
          const error = objectValue(event.error, "stream error");
          throw new ProviderError(String(error.message ?? error.type ?? "Anthropic stream error"));
        }
      }
    } catch (error) {
      if (request.signal.aborted) throw abortError();
      if (error instanceof ProviderError) throw error;
      throw new ProviderError(errorMessage(error));
    }
    throwIfAborted(request.signal);
    if (!terminal || !message) throw new ProviderError("Anthropic stream ended before a complete message");
    const reason = stringValue(message, "stop_reason", true);
    if (!["end_turn", "tool_use", "stop_sequence", "refusal"].includes(reason))
      throw new ProviderError("Anthropic response is incomplete: " + reason);
    const output: Item[] = [];
    const toolCalls: ToolCall[] = [];
    const ids = new Set<string>();
    const text: string[] = [];
    for (const [, block] of [...blocks.entries()].sort(([a], [b]) => a - b)) {
      if (block.open) throw new ProviderError("Anthropic stream ended with an unfinished content block");
      const value = block.value;
      if (value.type === "tool_use") {
        const callId = stringValue(value, "id", true);
        const name = stringValue(value, "name", true);
        const arguments_ = objectValue(value.input, "tool arguments");
        if (ids.has(callId)) throw new ProviderError("duplicate Anthropic tool call");
        ids.add(callId);
        if (utf8Length(JSON.stringify(arguments_)) > MAX_SSE_BYTES)
          throw new ProviderError("tool arguments exceed their byte limit");
        toolCalls.push({ callId, name, arguments: arguments_ });
      } else if (value.type === "text") {
        text.push(stringValue(value, "text"));
      }
      output.push(value);
    }
    const replay: Item = { ...message, content: output, usage };
    return {
      text: text.join(""), assistant: anthropicAssistant(output, toolCalls, replay), output, toolCalls,
      ...(typeof usage.input_tokens === "number" && typeof usage.output_tokens === "number"
        ? { usage: { inputTokens: usage.input_tokens, outputTokens: usage.output_tokens } } : {}),
    };
  }

  private async *httpEvents(payload: Item, signal: AbortSignal): AsyncGenerator<Item> {
    const key = this.options.apiKey ?? environment("ANTHROPIC_API_KEY");
    if (!key && !Object.keys(this.options.headers ?? {}).some((name) => name.toLowerCase() === "x-api-key"))
      throw new ProviderError("ANTHROPIC_API_KEY is not set");
    yield* httpSseEvents(
      this.options.endpoint ?? (this.options.baseUrl ? this.options.baseUrl.replace(/\/+$/, "") + "/messages" : ANTHROPIC_API),
      payload,
      {
        "x-api-key": key ?? "",
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      signal,
      this.options,
    );
  }
}

function anthropicInput(history: Item[]): Item[] {
  const messages: Item[] = [];
  for (const entry of history) {
    if (entry.role === "user") {
      messages.push({ role: "user", content: anthropicContent(entry.content ?? "") });
    } else if (entry.role === "assistant") {
      const metadata = isItem(entry.providerMetadata) ? entry.providerMetadata : undefined;
      const replay =
        metadata?.provider === "anthropic-messages" && isItem(metadata.message) ? metadata.message : undefined;
      if (replay && Array.isArray(replay.content)) {
        // The API rejects a message without content, so a turn that said nothing is left out.
        if (replay.content.length) messages.push({ role: "assistant", content: replay.content });
        continue;
      }
      if (!Array.isArray(entry.content)) continue;
      const blocks: Item[] = [];
      for (const block of entry.content) {
        if (!isItem(block)) continue;
        if (block.type === "text" && typeof block.text === "string") {
          blocks.push({ type: "text", text: block.text });
        } else if (block.type === "tool_call") {
          blocks.push({
            type: "tool_use",
            id: block.callId,
            name: block.name,
            input: block.arguments ?? {},
          });
        } else if (block.type === "tool_use" || block.type === "thinking" || block.type === "redacted_thinking") {
          blocks.push(block);
        }
      }
      if (blocks.length) messages.push({ role: "assistant", content: blocks });
    } else if (entry.role === "tool_result") {
      const result: Item = {
        type: "tool_result",
        tool_use_id: entry.callId,
        content: anthropicContent(entry.output ?? ""),
        is_error: Boolean(entry.isError),
      };
      const previous = messages.at(-1);
      if (
        previous?.role === "user" &&
        Array.isArray(previous.content) &&
        previous.content.every((block) => isItem(block) && block.type === "tool_result")
      ) {
        previous.content.push(result);
      } else {
        messages.push({ role: "user", content: [result] });
      }
    } else {
      // Accept Anthropic-native messages for migration.
      messages.push(entry);
    }
  }
  return messages;
}

/** Text stays a string; a list of parts becomes Messages content blocks. */
function anthropicContent(content: unknown): unknown {
  if (!isContentParts(content)) return content;
  return content.map((part) => part.type === "text" ? { type: "text", text: part.text }
    : { type: "image", source: "url" in part ? { type: "url", url: part.url }
      : { type: "base64", media_type: part.mediaType, data: part.data } });
}

function anthropicAssistant(output: Item[], calls: ToolCall[], replayMessage: Item): Item {
  const callsById = new Map(calls.map((call) => [call.callId, call]));
  const content: Item[] = [];
  for (const block of output) {
    if (block.type === "text" && typeof block.text === "string") {
      content.push({ type: "text", text: block.text });
    } else if (block.type === "thinking" || block.type === "redacted_thinking") {
      content.push({ type: "reasoning", ...(typeof block.thinking === "string" ? { text: block.thinking } : {}) });
    } else if (block.type === "tool_use" && typeof block.id === "string") {
      const call = callsById.get(block.id);
      if (call) content.push({ type: "tool_call", callId: call.callId, name: call.name, arguments: call.arguments });
    }
  }
  return {
    role: "assistant",
    content,
    providerMetadata: { provider: "anthropic-messages", message: replayMessage },
  };
}
