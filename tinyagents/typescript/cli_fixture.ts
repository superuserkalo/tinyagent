/** The shipped CLI with a scripted model, so its exit codes can be tested offline. */
import { ScriptedProvider, runCli, type ScriptedTurn } from "./tiny_agent.ts";

const script = JSON.parse(process.env.TINYAGENT_FIXTURE_SCRIPT ?? "[]") as ScriptedTurn[];
process.exitCode = await runCli({ argv: process.argv.slice(2), env: process.env, provider: new ScriptedProvider(script) });
