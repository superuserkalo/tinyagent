/**
 * One session across two processes: a tool, streaming, cancellation, restart,
 * resume, compaction, and continuing afterwards.
 *
 *   node examples/session.ts   # answers, cancels a second run at its tool call, exits
 *   node examples/session.ts   # reopens the session, continues, compacts, continues
 *
 * Set OPENAI_API_KEY first. ../example.test.ts runs this file offline against a local server.
 */
import { tmpdir } from "node:os";
import { join } from "node:path";
// In your own project, import these from "tiny-agent-sdk".
import { Agent, OpenAIProvider, Session, Tool, type AgentEvent, type ContextBudget } from "../tiny_agent.ts";

const forecast = new Tool({
  name: "forecast",
  description: "Return tomorrow's forecast for a city.",
  parameters: {
    type: "object",
    properties: { city: { type: "string" } },
    required: ["city"],
    additionalProperties: false,
  },
  handler: ({ city }) => ({ city, sky: "clear", highCelsius: 24, lowCelsius: 15, wind: "light northerly" }),
});

// A saved session reopens only with the model, instructions, and tools it was created with.
const agent = new Agent({
  model: process.env.TINYAGENT_MODEL ?? "gpt-5.6",
  instructions: "Answer weather questions. Call the forecast tool for every city you are asked about.",
  tools: [forecast],
  // TINYAGENT_ENDPOINT selects another OpenAI Responses URL; unset, this is api.openai.com.
  provider: new OpenAIProvider({ endpoint: process.env.TINYAGENT_ENDPOINT }),
});

// The session compacts by itself before a request would exceed maxTokens. You
// supply the counter; four characters per token is a workable estimate.
const budget: ContextBudget = {
  maxTokens: 8000,
  responseReserve: 1000,
  countTokens: (request) => Math.ceil(JSON.stringify(request).length / 4),
  keepTurns: 1,
};

function show(event: AgentEvent): void {
  if (event.type === "text_delta") process.stdout.write(event.delta);
  else if (event.type === "tool_execution_start") console.log(`[${event.toolName} ${JSON.stringify(event.arguments)}]`);
  else if (event.type === "agent_end") console.log(`\n[run ${event.status}]`);
}

/** First process: create a durable session, finish one run, and cancel the next. */
async function start(directory: string): Promise<void> {
  const session = new Session(agent, { directory, budget });
  try {
    for await (const event of session.stream("What is the forecast for Lisbon?")) show(event);

    // Cancel the second run as its tool call starts. A cancelled run commits nothing.
    const controller = new AbortController();
    try {
      for await (const event of session.stream("And for Porto?", { signal: controller.signal })) {
        show(event);
        if (event.type === "tool_execution_start") controller.abort();
      }
    } catch (error) {
      if (!controller.signal.aborted) throw error;
    }
    console.log(`[saved ${session.id}; run this again to resume it]`);
  } finally {
    // Closing releases the session's writer lock. It never deletes the session.
    await session.close();
  }
}

/** Second process: reopen the session and carry on. */
async function resume(directory: string, sessionId: string): Promise<void> {
  const session = Session.open(agent, directory, sessionId, budget);
  try {
    console.log(`[resumed ${session.id} with ${session.history.length} committed messages]`);

    // The cancelled run is in the journal, marked aborted, and is not part of the conversation.
    const cancelled = session.records.filter((record) => record.type === "run_failed").at(-1);
    console.log(`[the earlier run was ${cancelled?.status}]`);
    // Had the process died mid-run instead, session.resumable would be true and session.resume() would continue it.
    if (session.resumable) for await (const event of session.stream("And for Porto?")) show(event);
    for await (const event of session.stream("And for Porto?")) show(event);

    // Replace older turns in the model's context with a summary. The full history stays on disk.
    const compacted = await session.compact();
    console.log(`[compacted: ${compacted}; ${session.history.length} messages still in history]`);

    for await (const event of session.stream("Which of the two is the better day for a walk?")) show(event);
  } finally {
    await session.close();
  }
}

const directory = process.env.TINYAGENT_SESSION_DIR ?? join(tmpdir(), "tinyagent-example");
const saved = Session.list(directory).at(-1);
if (saved) await resume(directory, saved);
else await start(directory);
