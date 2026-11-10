import { Ajv2020, type ValidateFunction } from "ajv/dist/2020.js";

export type Item = Record<string, unknown>;
export type Transport = (payload: Item) => Promise<Item[]> | Item[];
export type EventTransport = (payload: Item, signal: AbortSignal) => AsyncIterable<Item>;
export type OpenAIEventTransport = EventTransport;
export type ToolHandler = (arguments_: Item, signal: AbortSignal) => unknown | Promise<unknown>;

export interface ToolOptions {
  name: string;
  description: string;
  parameters: Item;
  handler: ToolHandler;
}

// Validation is synchronous and local. $ref never triggers a network request.
const schemas = new Ajv2020({ strict: false, validateFormats: false, addUsedSchema: false, ownProperties: true });

export class Tool {
  readonly name: string;
  readonly description: string;
  readonly parameters: Item;
  readonly handler: ToolHandler;
  private readonly validate: ValidateFunction<unknown>;

  constructor(options: ToolOptions) {
    this.name = options.name;
    this.description = options.description;
    this.parameters = structuredClone(options.parameters);
    this.handler = options.handler;
    if (this.parameters.$async) throw new TypeError("Tool schemas must use synchronous validation");
    this.validate = schemas.compile<unknown>(this.parameters);
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

  execute(arguments_: Item, signal: AbortSignal): unknown | Promise<unknown> {
    if (!this.validate(arguments_)) {
      const error = this.validate.errors?.[0];
      throw new TypeError(
        `Invalid arguments for ${this.name}${error?.instancePath ?? ""}: ${error?.message ?? "schema mismatch"}`,
      );
    }
    return this.handler(arguments_, signal);
  }
}

export interface ToolCall {
  callId: string;
  name: string;
  arguments: Item;
}

export interface ProviderEvent {
  type: "text_delta";
  delta: string;
}

export interface ProviderTurn {
  text: string;
  assistant: Item;
  output: Item[];
  toolCalls: ToolCall[];
}

export interface ProviderRequest {
  model: string;
  instructions: string;
  history: Item[];
  tools: Tool[];
  signal: AbortSignal;
}

export interface Provider {
  stream(request: ProviderRequest): AsyncGenerator<ProviderEvent, ProviderTurn>;
}

interface EventBase {
  runId: string;
  sequence: number;
}

export type CompactionEventData =
  | { type: "compaction_start" }
  | { type: "compaction_end"; status: "completed"; changed: boolean }
  | { type: "compaction_end"; status: "failed" | "aborted"; error: string };

export type AgentEvent =
  | (EventBase & { type: "agent_start" })
  | (EventBase & CompactionEventData)
  | (EventBase & { type: "text_delta"; delta: string })
  | (EventBase & { type: "tool_execution_start"; toolCallId: string; toolName: string; arguments: Item })
  | (EventBase & { type: "tool_execution_end"; toolCallId: string; toolName: string; output: string; isError: boolean })
  | (EventBase & { type: "agent_end"; status: "completed"; text: string })
  | (EventBase & { type: "agent_end"; status: "failed"; error: { code: string; message: string; retryable: boolean } })
  | (EventBase & { type: "agent_end"; status: "aborted" });

export interface RunResult {
  text: string;
  history: Item[];
  output: Item[];
}

export interface RunOptions {
  signal?: AbortSignal;
  runId?: string;
  onEvent?: (event: AgentEvent) => void | Promise<void>;
}

export class ProviderError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ProviderError";
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
