#!/usr/bin/env node
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { realpathSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { createInterface } from "node:readline";
import { runRpcProcess } from "./rpc.ts";
import { serve } from "./http.ts";
import {
  Tool,
  ProviderError,
  ToolOutputLimitError,
  isItem,
  errorMessage,
  abortError,
  isAbortError,
  throwIfAborted,
  type Item,
  type Transport,
  type Provider,
  type ProviderTurn,
  type AgentEvent,
  type RunResult,
  type RunOptions,
  type CompactionEventData,
} from "./types.ts";
import { OpenAIProvider, CompleteTransportProvider } from "./openai.ts";
import { AnthropicProvider } from "./anthropic.ts";
import { Session, type ContextBudget } from "./session.ts";
export { Session, RecoveryRequiredError } from "./session.ts";
export type { ContextBudget, SessionOptions } from "./session.ts";
export { SessionBusyError, SessionCorruptionError } from "./storage.ts";

export { Tool, ProviderError, ToolOutputLimitError } from "./types.ts";
export type {
  Item,
  Transport,
  EventTransport,
  OpenAIEventTransport,
  ToolHandler,
  ToolOptions,
  ToolCall,
  ProviderEvent,
  ProviderTurn,
  ProviderRequest,
  Provider,
  AgentEvent,
  RunResult,
  RunOptions,
} from "./types.ts";
export { OpenAIProvider } from "./openai.ts";
export type { OpenAIProviderOptions } from "./openai.ts";
export { AnthropicProvider } from "./anthropic.ts";
export type { AnthropicProviderOptions } from "./anthropic.ts";
import { MAX_TOOL_OUTPUT_BYTES } from "./protocol.ts";

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

export const VERSION = "0.0.1";

const MODEL = process.env.TINYAGENT_MODEL ?? "gpt-5.6";
const INSTRUCTIONS = `You are TinyAgent, a coding agent in the current directory.
Inspect before changing files. Use bash for shell and file work. Each call is a fresh process.`;
const DENIED_WORDS = new Set([
  "rm",
  "rmdir",
  "unlink",
  "shred",
  "truncate",
  "dd",
  "mkfs",
  "wipefs",
  "fdisk",
  "parted",
  "sudo",
  "doas",
  "shutdown",
  "reboot",
  "poweroff",
  "halt",
  "kill",
  "pkill",
  "killall",
]);

export interface AgentOptions {
  model: string;
  instructions?: string;
  tools?: Tool[];
  apiKey?: string;
  provider?: Provider;
  transport?: Transport;
}

export class Agent {
  readonly model: string;
  readonly instructions: string;
  readonly tools: Map<string, Tool>;
  readonly provider: Provider;

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
  }

  async *stream(
    prompt: string,
    history: Item[] = [],
    options: Pick<RunOptions, "signal" | "runId"> = {},
    hooks: { record?: (record: Item) => void;
      prepare?: (history: Item[]) => AsyncGenerator<CompactionEventData, Item[]> } = {},
  ): AsyncGenerator<AgentEvent, RunResult> {
    const controller = new AbortController();
    const signal = options.signal ? AbortSignal.any([options.signal, controller.signal]) : controller.signal;
    let transcript = [...structuredClone(history), { role: "user", content: prompt }];
    const runId = options.runId ?? `r_${randomUUID().replaceAll("-", "")}`;
    let sequence = 0;
    let terminal = false;
    const event = <T extends Omit<AgentEvent, "runId" | "sequence">>(value: T): AgentEvent =>
      ({ ...value, runId, sequence: ++sequence }) as AgentEvent;

    try {
      hooks.record?.({ type: "item", item: transcript[transcript.length - 1] });
      yield event({ type: "agent_start" });
      while (true) {
        throwIfAborted(signal);
        if (hooks.prepare) {
          const preparation = hooks.prepare(transcript);
          try {
            while (true) {
              const next = await preparation.next();
              if (next.done) { transcript = next.value; break; }
              yield event(next.value);
            }
          } finally { await preparation.return(undefined as never); }
          throwIfAborted(signal);
        }
        const providerStream = this.provider.stream({
          model: this.model,
          instructions: this.instructions,
          history: transcript,
          tools: [...this.tools.values()],
          signal,
        });
        let turn: ProviderTurn;
        let providerStopped = false;
        try {
          while (true) {
            const next = await providerStream.next();
            throwIfAborted(signal);
            if (next.done) {
              turn = next.value;
              providerStopped = true;
              break;
            }
            if (next.value.type === "text_delta") yield event({ type: "text_delta", delta: next.value.delta });
          }
        } catch (error) {
          providerStopped = true;
          throw error;
        } finally {
          // Closing a stream must release its provider even when the consumer stops early.
          if (!providerStopped) controller.abort();
          await providerStream.return(undefined as never);
        }
        transcript.push(turn.assistant);
        hooks.record?.({ type: "item", item: turn.assistant });
        if (turn.toolCalls.length === 0) {
          const result = { text: turn.text, history: transcript, output: turn.output };
          terminal = true;
          yield event({ type: "agent_end", status: "completed", text: turn.text });
          throwIfAborted(signal);
          return result;
        }
        for (const call of turn.toolCalls) {
          const tool = this.tools.get(call.name);
          if (!tool) throw new ProviderError(`unknown tool: ${call.name}`);
          hooks.record?.({ type: "tool_intent", callId: call.callId, toolName: call.name, arguments: call.arguments });
          yield event({
            type: "tool_execution_start",
            toolCallId: call.callId,
            toolName: call.name,
            arguments: call.arguments,
          });
          let output: string;
          let isError = false;
          try {
            throwIfAborted(signal);
            const value = await tool.execute(call.arguments, signal);
            throwIfAborted(signal);
            const serialized = typeof value === "string" ? value : JSON.stringify(value);
            if (serialized === undefined) throw new Error("tool returned no output");
            checkToolOutput(serialized);
            output = serialized;
          } catch (error) {
            if (signal.aborted || isAbortError(error)) {
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
            output = `${error instanceof Error ? error.name : "Error"}: ${errorMessage(error)}`;
            if (Buffer.byteLength(output, "utf8") > MAX_TOOL_OUTPUT_BYTES)
              output = `ToolOutputLimitError: tool error exceeds ${MAX_TOOL_OUTPUT_BYTES} UTF-8 bytes`;
          }
          const observation = { role: "tool_result", callId: call.callId, toolName: call.name, output, isError };
          hooks.record?.({ type: "item", item: observation });
          yield event({
            type: "tool_execution_end",
            toolCallId: call.callId,
            toolName: call.name,
            output,
            isError,
          });
          transcript.push(observation);
        }
      }
    } catch (error) {
      if (terminal) throw error;
      if (signal.aborted || isAbortError(error)) {
        yield event({ type: "agent_end", status: "aborted" });
        throw abortError();
      }
      yield event({
        type: "agent_end",
        status: "failed",
        error: { code: "provider_error", message: errorMessage(error), retryable: false },
      });
      throw error;
    } finally {
      controller.abort();
    }
  }

  async run(prompt: string, history: Item[] = [], options: RunOptions = {}): Promise<RunResult> {
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

export function deniedReason(command: string): string | undefined {
  const normalized = command.toLowerCase().replace(/[\\"']/g, "");
  const words = new Set(normalized.match(/[a-z0-9_.-]+/g) ?? []);
  const blocked = [...words].filter((word) => DENIED_WORDS.has(word) || word.startsWith("mkfs.")).sort();
  if (blocked.length) return `'${blocked[0]}' is denied`;
  if (words.has("find") && words.has("-delete")) return "'find -delete' is denied";
  if (
    words.has("git") &&
    (words.has("clean") ||
      words.has("restore") ||
      (words.has("reset") && words.has("--hard")) ||
      (words.has("checkout") && words.has("--")))
  )
    return "destructive git command is denied";
  if (normalized.replace(/\s/g, "").includes(":(){:|:&};:")) return "fork bomb is denied";
  return undefined;
}

export async function runBash(command: string, signal?: AbortSignal): Promise<string> {
  const reason = deniedReason(command);
  if (reason) return `exit_code: 126\nstdout:\n\nstderr:\nblocked: ${reason}\n`;
  if (signal) throwIfAborted(signal);
  const child = spawn("bash", ["-c", command], {
    detached: process.platform !== "win32",
    stdio: ["ignore", "pipe", "pipe"],
  });
  const stop = () => {
    try {
      if (child.pid && process.platform !== "win32") process.kill(-child.pid, "SIGKILL");
      else child.kill("SIGKILL");
    } catch {
      try {
        child.kill("SIGKILL");
      } catch {
        // The process already exited.
      }
    }
  };
  const stdoutChunks: Buffer[] = [];
  const stderrChunks: Buffer[] = [];
  let capturedBytes = 0;
  let outputTooLarge = false;
  const capture = (chunks: Buffer[], chunk: Buffer) => {
    if (outputTooLarge) return;
    if (capturedBytes + chunk.length > MAX_TOOL_OUTPUT_BYTES) {
      outputTooLarge = true;
      stop();
      return;
    }
    capturedBytes += chunk.length;
    chunks.push(chunk);
  };
  child.stdout.on("data", (chunk: Buffer) => capture(stdoutChunks, chunk));
  child.stderr.on("data", (chunk: Buffer) => capture(stderrChunks, chunk));

  return await new Promise<string>((resolve, reject) => {
    const cleanup = () => signal?.removeEventListener("abort", stop);
    signal?.addEventListener("abort", stop, { once: true });
    if (signal?.aborted) stop();
    child.once("error", (error) => {
      cleanup();
      reject(error);
    });
    child.once("close", (code) => {
      cleanup();
      if (signal?.aborted) reject(abortError());
      else if (outputTooLarge) {
        reject(new ToolOutputLimitError(`tool output exceeds ${MAX_TOOL_OUTPUT_BYTES} UTF-8 bytes`));
      } else {
        const stdout = Buffer.concat(stdoutChunks).toString("utf8");
        const stderr = Buffer.concat(stderrChunks).toString("utf8");
        const observation = `exit_code: ${code ?? -1}\nstdout:\n${stdout}stderr:\n${stderr}`;
        try {
          checkToolOutput(observation);
          resolve(observation);
        } catch (error) {
          reject(error);
        }
      }
    });
  });
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

async function main(): Promise<void> {
  if (process.argv.includes("--self-test")) return selfTest();
  const directory = process.env.TINYAGENT_SESSION_DIR ?? ".tinyagent/sessions";
  if (process.argv.includes("--list-sessions")) {
    console.log(Session.list(directory).join("\n"));
    return;
  }
  const resumeIndex = process.argv.indexOf("--resume");
  const sessionId = resumeIndex < 0 ? undefined : process.argv[resumeIndex + 1];
  if (resumeIndex >= 0 && !sessionId) throw new Error("--resume requires a session ID");
  const budget: ContextBudget | undefined = process.env.TINYAGENT_CONTEXT_TOKENS ? {
    maxTokens: Number(process.env.TINYAGENT_CONTEXT_TOKENS),
    responseReserve: Number(process.env.TINYAGENT_RESPONSE_RESERVE ?? 4096),
    countTokens: (request) => Buffer.byteLength(JSON.stringify(request)) + 1024,
  } : undefined;
  const providerName = (process.env.TINYAGENT_PROVIDER ?? "openai").toLowerCase();
  let provider: Provider;
  let model: string;
  if (providerName === "anthropic") {
    if (!process.env.ANTHROPIC_API_KEY) throw new Error("ANTHROPIC_API_KEY is not set");
    provider = new AnthropicProvider();
    model = process.env.TINYAGENT_MODEL ?? "claude-sonnet-4-6";
  } else if (providerName === "openai") {
    if (!process.env.OPENAI_API_KEY) throw new Error("OPENAI_API_KEY is not set");
    provider = new OpenAIProvider();
    model = MODEL;
  } else {
    throw new Error(`unsupported TINYAGENT_PROVIDER: ${providerName}`);
  }
  const bash = new Tool({
    name: "bash",
    description: "Run a non-interactive bash command in the current directory.",
    parameters: {
      type: "object",
      properties: { command: { type: "string" } },
      required: ["command"],
      additionalProperties: false,
    },
    handler: async ({ command }, signal) => {
      if (typeof command !== "string") throw new Error("bash requires a string command");
      console.error(`\n$ ${command}`);
      const observation = await runBash(command, signal);
      console.error(observation);
      return observation;
    },
  });
  const agent = new Agent({ model, instructions: INSTRUCTIONS, tools: [bash], provider });
  if (process.argv[2] === "serve") {
    const server = await serve(agent, {
      host: process.env.TINYAGENT_HOST,
      port: process.env.TINYAGENT_PORT === undefined ? undefined : Number(process.env.TINYAGENT_PORT),
      token: process.env.TINYAGENT_TOKEN,
      allowRemote: process.env.TINYAGENT_ALLOW_REMOTE === "1",
      directory,
      budget,
    });
    console.error(`TinyAgent listening at ${server.url}`);
    const shutdown = () => {
      void server.close().finally(() => {
        process.off("SIGINT", shutdown);
        process.off("SIGTERM", shutdown);
      });
    };
    process.on("SIGINT", shutdown);
    process.on("SIGTERM", shutdown);
    return;
  }
  const modeIndex = process.argv.indexOf("--mode");
  if (modeIndex >= 0) {
    if (process.argv[modeIndex + 1] !== "rpc") throw new Error("supported mode: rpc");
    return runRpcProcess(agent, { directory, sessionId, resume: sessionId !== undefined, budget });
  }
  const session = new Session(agent, { directory, sessionId, resume: sessionId !== undefined, budget });
  let active: AbortController | undefined;
  let exiting = false;
  const lines = createInterface({ input: process.stdin, output: process.stdout });
  const interrupt = () => {
    exiting = true;
    active?.abort();
    lines.close();
  };
  process.on("SIGINT", interrupt);
  console.log(`TinyAgent session ${session.id}. Type a task, or Ctrl-C or Ctrl-D to exit.`);
  process.stdout.write("\n> ");
  try {
    for await (const line of lines) {
      const prompt = line.trim();
      if (prompt) {
        active = new AbortController();
        try {
          if (prompt === "/history") console.log(JSON.stringify(session.records, null, 2));
          else if (prompt.startsWith("/reconcile ")) session.reconcile(prompt.slice("/reconcile ".length));
          else if (prompt === "/compact") await session.compact({ signal: active.signal });
          else await session.run(prompt, {
            signal: active.signal,
            onEvent: (event) => {
              if (event.type === "text_delta") process.stdout.write(event.delta);
            },
          });
          process.stdout.write("\n");
        } catch (error) {
          if (!active.signal.aborted) console.error(`error: ${errorMessage(error)}`);
        } finally {
          active = undefined;
        }
      }
      if (exiting) break;
      process.stdout.write("\n> ");
    }
  } finally {
    process.off("SIGINT", interrupt);
    await session.close();
  }
  process.stdout.write("\n");
}

/** True when this file is the program, including through an installed command's symlink. */
function isProgram(): boolean {
  try {
    return import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href;
  } catch {
    return false;
  }
}

if (isProgram()) await main();
