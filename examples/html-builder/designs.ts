import { mkdirSync, readFileSync, readdirSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { Tool, ToolError } from "../../tinyagents/typescript/sdk.ts";

/** One published version, as stored in `<directory>/<sessionId>/<sequence>.json`. */
export interface Version {
  version: number;
  title: string;
  summary: string;
  /** The execution that wrote this version. Unique per version, by construction. */
  executionId: string;
  /** How many times the handler wrote this file. More than 1 means the call was run again after a crash. */
  attempts: number;
  bytes: number;
  createdAt: string;
  html: string;
}

const SESSION_ID = /^s_[A-Za-z0-9]{1,64}$/;
const MAX_HTML_BYTES = 512 * 1024;

function read(file: string): Version | undefined {
  try {
    return JSON.parse(readFileSync(file, "utf8")) as Version;
  } catch {
    return undefined;
  }
}

/** A session's versions, oldest first. An unknown session has none. */
export function listVersions(directory: string, sessionId: string): Version[] {
  if (!SESSION_ID.test(sessionId)) return [];
  let names: string[];
  try {
    names = readdirSync(join(directory, sessionId));
  } catch {
    return [];
  }
  return names.filter((name) => /^\d+\.json$/.test(name))
    .map((name) => read(join(directory, sessionId, name)))
    .filter((version): version is Version => version !== undefined)
    .sort((a, b) => a.version - b.version);
}

/**
 * The agent's one tool. `delay`, when given, holds a publish open for that
 * many milliseconds after its file is written: scripted mode uses it to make a
 * step slow enough to kill the server in the middle of.
 */
export function publishTool(directory: string, delay?: (version: number) => number): Tool {
  return new Tool({
    name: "publish_design",
    description: "Publish a complete, self-contained HTML document as the next numbered version of the design. " +
      "The user sees it at once in the preview. Call it again with a whole new document to revise.",
    parameters: {
      type: "object",
      properties: {
        title: { type: "string", description: "A short name for this version, a few words." },
        summary: { type: "string", description: "One sentence: what this version is or what changed." },
        html: { type: "string", description: "The whole document, from <!doctype html> to </html>, with CSS and JavaScript inline." },
      },
      required: ["title", "summary", "html"],
      additionalProperties: false,
    },
    // The teaching point of this demo.
    //
    // TinyAgent journals a tool call before the handler runs and again when it
    // returns. If the process dies in between, the journal cannot say whether
    // the effect happened. `idempotent: true` is this tool's promise that it is
    // safe to find out by running it again, and `session.resume()` then does
    // exactly that, handing the handler the SAME executionId as the first time.
    //
    // So the executionId, not a counter, decides which file is written. A
    // rerun finds the file its first attempt left, keeps that version number
    // and overwrites it. Had the handler asked "how many versions exist? write
    // the next one", a crash after the write would have produced the same
    // design twice under two numbers.
    idempotent: true,
    timeoutSeconds: 60,
    handler: async ({ title, summary, html }, signal, { executionId }) => {
      // In a Session the executionId is "<sessionId>:<journal sequence of the call>".
      const [sessionId, sequence] = executionId.split(":");
      if (!SESSION_ID.test(sessionId) || !/^\d+$/.test(sequence ?? ""))
        throw new ToolError("publish_design only works inside a session.");
      const text = String(html);
      if (!/<html[\s>]/i.test(text))
        throw new ToolError("html must be a complete document: <!doctype html><html>...</html>.");
      const bytes = Buffer.byteLength(text);
      if (bytes > MAX_HTML_BYTES) throw new ToolError(`html is ${bytes} bytes; the limit is ${MAX_HTML_BYTES}.`);

      const folder = join(directory, sessionId);
      mkdirSync(folder, { recursive: true });
      const file = join(folder, `${sequence}.json`);
      const earlier = read(file);
      const record: Version = {
        // Only a call never seen before takes a new number.
        version: earlier?.version ?? listVersions(directory, sessionId).length + 1,
        title: String(title).slice(0, 120),
        summary: String(summary).slice(0, 400),
        executionId,
        attempts: (earlier?.attempts ?? 0) + 1,
        bytes,
        createdAt: earlier?.createdAt ?? new Date().toISOString(),
        html: text,
      };
      // Write beside the file and rename, so a crash never leaves half a version.
      writeFileSync(`${file}.tmp`, JSON.stringify(record));
      renameSync(`${file}.tmp`, file);

      // The demo's slow step sits after the write on purpose: a kill here is
      // the worst case, a finished effect the journal knows nothing about.
      const wait = delay?.(record.version) ?? 0;
      if (wait > 0) await sleep(wait, undefined, { signal });
      return `Published version ${record.version}: "${record.title}" (${bytes} bytes).`;
    },
  });
}
