// The package's browser entry, "tiny-agent-sdk/browser": the agent loop, sessions,
// storage and providers, and nothing that needs Node. It runs anywhere with
// fetch, Web Crypto, timers and AbortSignal.any: browsers, Node, Deno, Bun and workers.
// Not here, because they need Node: FileStorage and the `directory` option,
// Session.open and Session.list, the bash tool, RPC, the HTTP server and the CLI.
// browser.test.ts checks that nothing reachable from this file imports Node.
export { Agent } from "./agent.ts";
export type { AgentOptions } from "./agent.ts";
export { Session, RecoveryRequiredError, ResumeError, SessionFailedError } from "./session.ts";
export type { ContextBudget, SessionOptions, EventPage } from "./session.ts";
export { SessionBusyError, SessionCorruptionError, FencedError, MemoryStorage, checkStorage } from "./storage.ts";
export type { Storage } from "./storage.ts";

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

export {
  PROTOCOL_VERSION,
  VERSION,
  MAX_JSONL_LINE_BYTES,
  MAX_HTTP_BODY_BYTES,
  MAX_QUEUED_OUTPUT_BYTES,
  MAX_TOOL_OUTPUT_BYTES,
} from "./protocol.ts";
