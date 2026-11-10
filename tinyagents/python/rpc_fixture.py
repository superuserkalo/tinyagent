"""A native RPC executable with deterministic provider work and real Bash."""

import json
import shlex
import sys
import threading

from tinyagent import Agent, ProviderEvent, ProviderTurn, Tool, ToolCall, run_rpc
from tiny_agent import run_bash


def diagnostic(**data):
    print(json.dumps(data), file=sys.stderr, flush=True)


class Provider:
    def stream(self, model, instructions, history, tools, token):
        prompt = history[-1].get("content")
        if prompt == "bash":
            path = shlex.quote(sys.argv[1])
            command = f'sleep 30 & printf "%s %s\\n" "$$" "$!" > {path}; wait'
            yield from ()
            return ProviderTurn(
                "",
                {"role": "assistant", "content": []},
                [],
                [ToolCall("bash-1", "bash", {"command": command})],
            )
        if prompt in ("wait", "flood"):
            stopped = threading.Event()
            remove = token.on_cancel(stopped.set)
            produced = 0
            diagnostic(started=prompt)
            try:
                if prompt == "flood":
                    for _ in range(100):
                        produced += 1
                        yield ProviderEvent("text_delta", "x" * 100_000)
                        token.raise_if_cancelled()
                stopped.wait()
                token.raise_if_cancelled()
            finally:
                remove()
                diagnostic(closed=prompt, cancelled=token.cancelled, produced=produced)
        yield from ()
        return ProviderTurn("done", {"role": "assistant", "content": "done"}, [], [])


agent = Agent(
    model="test",
    provider=Provider(),
    tools=[
        Tool(
            "bash",
            "Run Bash",
            {"type": "object"},
            lambda arguments, token: run_bash(arguments["command"], token),
        )
    ],
)
try:
    run_rpc(agent)
except OSError as error:
    diagnostic(output_error=str(error))
diagnostic(driver_closed=True)
