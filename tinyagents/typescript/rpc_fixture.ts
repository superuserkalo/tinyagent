import { Agent, Tool, runBash, type Provider, type ProviderTurn } from "./tiny_agent.ts";
import { runRpcProcess } from "./rpc.ts";

const empty: ProviderTurn = {
  text: "",
  assistant: { role: "assistant", content: [] },
  output: [],
  toolCalls: [],
};
const provider: Provider = {
  async *stream(request) {
    if (request.history.at(-1)?.content === "bash") {
      const pidFile = process.env.TINYAGENT_FIXTURE_PID_FILE;
      if (!pidFile) throw new Error("fixture PID file is missing");
      const quoted = "'" + pidFile.replaceAll("'", "'\\''") + "'";
      return {
        ...empty,
        toolCalls: [
          {
            callId: "bash-child",
            name: "bash",
            arguments: { command: 'sleep 30 & printf \'%s %s\' "$$" "$!" > ' + quoted + "; wait" },
          },
        ],
      };
    }
    try {
      yield { type: "text_delta", delta: "ready" };
      if (request.history.at(-1)?.content === "flood") {
        process.stderr.write("flood ready\n");
        const delta = "x".repeat(131_072);
        while (!request.signal.aborted) yield { type: "text_delta", delta };
      } else if (!request.signal.aborted)
        await new Promise<void>((resolve) => request.signal.addEventListener("abort", () => resolve(), { once: true }));
      return empty;
    } finally {
      process.stderr.write("provider released\n");
    }
  },
};
const bash = new Tool({
  name: "bash",
  description: "Run the native Bash cancellation fixture.",
  parameters: {
    type: "object",
    properties: { command: { type: "string" } },
    required: ["command"],
    additionalProperties: false,
  },
  handler({ command }, signal) {
    if (typeof command !== "string") throw new TypeError("command must be a string");
    return runBash(command, signal);
  },
});
await runRpcProcess(new Agent({ model: "fixture", provider, tools: [bash] }));
