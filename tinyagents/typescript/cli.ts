// The coding CLI: runCli, which tiny_agent.ts calls with the process's arguments. Node only.
import assert from "node:assert/strict";
import { createInterface } from "node:readline";
import type { Readable, Writable } from "node:stream";
import { Agent } from "./agent.ts";
import { runRpcProcess } from "./rpc.ts";
import { serve } from "./http.ts";
import { errorMessage, type AgentEvent, type Provider } from "./types.ts";
import { OpenAIProvider } from "./openai.ts";
import { AnthropicProvider } from "./anthropic.ts";
import { ChatCompletionsProvider } from "./chat.ts";
import { type ContextBudget } from "./session.ts";
import { FileStorage, Session } from "./file_storage.ts";
import { bashTool, deniedReason, type Approve } from "./bash.ts";

const INSTRUCTIONS = `You are TinyAgent, a coding agent in the current directory.
Inspect before changing files. Use bash for shell and file work. Each call is a fresh process.`;

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
