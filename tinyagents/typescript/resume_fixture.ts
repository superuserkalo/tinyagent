/**
 * The agent the resume scenarios share, and a program that runs it until a
 * crash point. TINYAGENT_TEST_CRASH_AT is honoured here and nowhere in the SDK.
 *
 *   node resume_fixture.ts <directory> <sessionId> <effects log> [idempotent] [messageId]
 */
import { appendFileSync, realpathSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { Agent, FileStorage, ScriptedProvider, Session, Tool, type Item, type Storage } from "./tiny_agent.ts";

/** One tool call, then a final answer. The tool writes its execution ID to `log` each time it really runs. */
export function resumeAgent(log: string, options: { idempotent?: boolean; turns?: number; crashAt?: string } = {}):
  { agent: Agent; provider: ScriptedProvider } {
  const effect = new Tool({
    name: "effect",
    description: "Leave a mark.",
    parameters: { type: "object", properties: { mark: { type: "string" } }, required: ["mark"] },
    idempotent: options.idempotent ?? false,
    handler: ({ mark }, _signal, { executionId }) => {
      appendFileSync(log, `${executionId}\n`);
      if (options.crashAt === "after_intent") process.exit(41);
      return `marked ${String(mark)}`;
    },
  });
  const script = [[{ callId: "call_1", name: "effect", arguments: { mark: "x" } }], "all done"];
  const provider = new ScriptedProvider(script.slice(2 - (options.turns ?? 2)));
  return { agent: new Agent({ model: "scripted", tools: [effect], provider }), provider };
}

/** A FileStorage that kills the process at the named point of the journal. */
function crashingStorage(directory: string, crashAt: string | undefined): Storage {
  const files = new FileStorage(directory);
  return {
    load: (id) => files.load(id),
    list: () => files.list(),
    release: (id) => files.release(id),
    append(id, expected, record) {
      const item = record.item as Item | undefined;
      if (crashAt === "before_intent" && record.type === "tool_intent") process.exit(41);
      const sequence = files.append(id, expected, record);
      if (crashAt === "after_result" && record.type === "tool_result") process.exit(41);
      if (crashAt === "after_reply" && item?.role === "assistant" && !record.toolCalls) process.exit(41);
      return sequence;
    },
  };
}

function isProgram(): boolean {
  try { return import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href; } catch { return false; }
}

if (isProgram()) {
  const [directory, sessionId, log, idempotent, messageId] = process.argv.slice(2);
  const crashAt = process.env.TINYAGENT_TEST_CRASH_AT;
  const { agent } = resumeAgent(log, { idempotent: idempotent === "idempotent", crashAt });
  const session = new Session(agent, { storage: crashingStorage(directory, crashAt), sessionId });
  await session.run("leave a mark", messageId ? { messageId } : {});
  await session.close();
}
