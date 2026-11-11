// The package's main entry, for Node. Everything a browser can run is also
// exported, without the Node-only parts, by browser.ts.
export { Agent } from "./agent.ts";
export type { AgentOptions } from "./agent.ts";
// This Session also takes a `directory`, and has Session.open and Session.list: they need files.
export { Session, FileStorage } from "./file_storage.ts";
export { RecoveryRequiredError, ResumeError, SessionFailedError } from "./session.ts";
export type { ContextBudget, SessionOptions, EventPage } from "./session.ts";
export { SessionBusyError, SessionCorruptionError, FencedError, MemoryStorage, checkStorage } from "./storage.ts";
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

export { VERSION } from "./protocol.ts";
export { runCli } from "./cli.ts";
export type { CliOptions } from "./cli.ts";
