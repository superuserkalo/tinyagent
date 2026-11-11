import { spawn } from "node:child_process";
import { MAX_TOOL_OUTPUT_BYTES } from "./protocol.ts";
import { Tool, ToolError, ToolOutputLimitError, abortError, throwIfAborted, withTimeout } from "./types.ts";

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

/** Output beyond this is cut to its first and last KEPT_BYTES. */
const MAX_BASH_OUTPUT_BYTES = 30_000;
const KEPT_BYTES = MAX_BASH_OUTPUT_BYTES / 2;

export interface BashOptions {
  /** Working directory for every command. Default: the process's. */
  cwd?: string;
  /** Variables merged over the process environment. */
  env?: Record<string, string>;
  /** Kill the command's process group after this long. Default: no limit. */
  timeoutSeconds?: number;
}

/**
 * Decide whether a command may run. Return true to run it. Return false, or a
 * string giving the reason, to reject it; the model sees the rejection.
 */
export type Approve = (command: string, signal: AbortSignal) => boolean | string | Promise<boolean | string>;

/** The guard against accidents. It is not a sandbox. */
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

/** Keeps the start and the end of a stream in bounded memory. */
class Capture {
  private head = Buffer.alloc(0);
  private tail = Buffer.alloc(0);
  private total = 0;

  add(chunk: Buffer): void {
    this.total += chunk.length;
    const room = KEPT_BYTES - this.head.length;
    if (room > 0) {
      this.head = Buffer.concat([this.head, chunk.subarray(0, room)]);
      chunk = chunk.subarray(room);
    }
    if (chunk.length) this.tail = Buffer.from(Buffer.concat([this.tail, chunk]).subarray(-KEPT_BYTES));
  }

  text(): string {
    if (this.total <= MAX_BASH_OUTPUT_BYTES) return Buffer.concat([this.head, this.tail]).toString("utf8");
    // Cut on UTF-8 boundaries: drop a character split at the end of the head or the start of the tail.
    let end = this.head.length;
    let lead = end - 1;
    while (lead > 0 && (this.head[lead] & 0xc0) === 0x80) lead--;
    const first = this.head[lead];
    const length = first >= 0xf0 ? 4 : first >= 0xe0 ? 3 : first >= 0xc0 ? 2 : 1;
    if (lead + length > end) end = lead;
    let start = 0;
    while (start < this.tail.length && (this.tail[start] & 0xc0) === 0x80) start++;
    const elided = this.total - end - (this.tail.length - start);
    return `${this.head.toString("utf8", 0, end)}\n[... ${elided} bytes elided ...]\n${this.tail.toString("utf8", start)}`;
  }
}

/**
 * Run one command with `bash -c` in its own process group. Cancellation and
 * the timeout kill the whole group. A timeout rejects with a ToolError holding
 * the output so far and a final line `[timed out after N s]`.
 */
export async function runBash(command: string, signal?: AbortSignal, options: BashOptions = {}): Promise<string> {
  const reason = deniedReason(command);
  if (reason) throw new ToolError(`exit_code: 126\nstdout:\n\nstderr:\nblocked: ${reason}\n`);
  return await withTimeout(options.timeoutSeconds, signal ?? new AbortController().signal,
    (scoped) => execute(command, scoped, options, signal));
}

/** `signal` stops the command; when `cancel` was not what fired it, the stop was the timeout. */
function execute(command: string, signal: AbortSignal, options: BashOptions, cancel?: AbortSignal): Promise<string> {
  throwIfAborted(signal);
  const child = spawn("bash", ["-c", command], {
    cwd: options.cwd,
    env: options.env ? { ...process.env, ...options.env } : undefined,
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
  const stdout = new Capture();
  const stderr = new Capture();
  // Decoded first and truncated second: bytes that are not UTF-8 become U+FFFD before they are counted.
  const decoders = [new TextDecoder(), new TextDecoder()];
  child.stdout.on("data", (chunk: Buffer) => stdout.add(Buffer.from(decoders[0].decode(chunk, { stream: true }))));
  child.stderr.on("data", (chunk: Buffer) => stderr.add(Buffer.from(decoders[1].decode(chunk, { stream: true }))));
  // Once the shell is gone or the call is stopped, whoever still holds the pipes (a background
  // job, a daemon in another process group) gets one second. Then the call stops listening.
  let closing: ReturnType<typeof setTimeout> | undefined;
  const closePipes = () => {
    closing ??= setTimeout(() => { child.stdout.destroy(); child.stderr.destroy(); }, 1000);
  };

  return new Promise<string>((resolve, reject) => {
    const halt = () => { stop(); closePipes(); };
    const cleanup = () => { signal.removeEventListener("abort", halt); clearTimeout(closing); };
    signal.addEventListener("abort", halt, { once: true });
    if (signal.aborted) halt();
    child.once("error", (error) => {
      cleanup();
      reject(error);
    });
    child.once("exit", () => {
      // The shell can fork a foreground command between the first group kill
      // and its own exit. Kill again once it can no longer fork, or that child
      // can survive with the pipes open and prevent `close` from ever firing.
      closePipes();
      if (!signal.aborted) return;
      // A child in the middle of an exec can be missed by one group signal. Keep at it until the group is empty.
      let attempts = 0;
      const sweep = setInterval(() => {
        stop();
        let empty = !child.pid;
        try { if (child.pid) process.kill(-child.pid, 0); } catch { empty = true; }
        if (empty || ++attempts >= 40) clearInterval(sweep);
      }, 25);
      stop();
    });
    child.once("close", (code) => {
      cleanup();
      const timedOut = signal.aborted && !cancel?.aborted && options.timeoutSeconds !== undefined;
      const observation = `exit_code: ${timedOut ? 124 : code ?? -1}\nstdout:\n${stdout.text()}stderr:\n${stderr.text()}`;
      if (cancel?.aborted || (signal.aborted && !options.timeoutSeconds)) reject(abortError());
      else if (signal.aborted)
        reject(new ToolError(`${observation}${observation.endsWith("\n") ? "" : "\n"}[timed out after ${options.timeoutSeconds} s]`));
      else if (Buffer.byteLength(observation, "utf8") > MAX_TOOL_OUTPUT_BYTES)
        reject(new ToolOutputLimitError(`tool output exceeds ${MAX_TOOL_OUTPUT_BYTES} UTF-8 bytes`));
      else resolve(observation);
    });
  });
}

/**
 * The guarded bash tool. `approve` is asked before each command the blacklist
 * lets through; it is a `beforeTool` hook for this one tool.
 */
export function bashTool(options: BashOptions & { approve?: Approve } = {}): Tool {
  const { approve, ...bash } = options;
  return new Tool({
    name: "bash",
    description: "Run a non-interactive bash command in the current directory.",
    parameters: {
      type: "object",
      properties: { command: { type: "string" } },
      required: ["command"],
      additionalProperties: false,
    },
    beforeTool: approve && (async (call, context) => {
      const command = String(call.arguments.command);
      if (deniedReason(command)) return undefined;
      const verdict = await approve(command, context.signal);
      if (verdict === true) return undefined;
      return { deny: `Rejected by user${typeof verdict === "string" && verdict ? `: ${verdict}` : ""}` };
    }),
    handler: ({ command }, signal) => runBash(String(command), signal, bash),
  });
}
