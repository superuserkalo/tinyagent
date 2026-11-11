"""Testing utilities: a provider that replays a script instead of calling a model."""

from __future__ import annotations

import copy
from typing import Any, Generator, Iterable

from tinyagent_types import (CancellationToken, JsonObject, ProviderEvent, ProviderTurn, Tool, ToolCall)


class ScriptedProvider:
    """Answers each model request with the next turn of a script.

    A turn is a string (the assistant's text), a list of tool calls (each a
    ToolCall or {"name": ..., "arguments": ...}), an exception to raise, or a
    dict with any of "text", "calls", "reasoning", "input_tokens" and
    "output_tokens". Every request is kept in `requests` for assertions.
    """

    def __init__(self, turns: Iterable[Any]) -> None:
        self.turns = list(turns)
        self.requests: list[JsonObject] = []

    def stream(self, model: str, instructions: str, history: list[JsonObject], tools: Iterable[Tool],
               cancellation: CancellationToken, extra: JsonObject | None = None,
               ) -> Generator[ProviderEvent, None, ProviderTurn]:
        cancellation.raise_if_cancelled()
        self.requests.append({"model": model, "instructions": instructions,
                              "history": copy.deepcopy(history),
                              "tools": [tool.name for tool in tools], "extra": extra})
        if not self.turns:
            raise AssertionError(f"ScriptedProvider has no turn for request {len(self.requests)}")
        turn = self.turns.pop(0)
        if isinstance(turn, BaseException):
            raise turn
        if not isinstance(turn, dict):
            turn = {"text": turn} if isinstance(turn, str) else {"calls": turn}
        text = turn.get("text", "")
        calls = [call if isinstance(call, ToolCall)
                 else ToolCall(call.get("id", f"call_{len(self.requests)}_{index}"), call["name"],
                               call.get("arguments", {}))
                 for index, call in enumerate(turn.get("calls", ()))]
        if turn.get("reasoning"):
            yield ProviderEvent("reasoning_delta", turn["reasoning"])
        if text:
            yield ProviderEvent("text_delta", text)
        content = [{"type": "text", "text": text}] if text else []
        content += [{"type": "tool_call", "callId": call.call_id, "name": call.name, "arguments": call.arguments}
                    for call in calls]
        return ProviderTurn(text, {"role": "assistant", "content": content}, [], calls,
                            turn.get("input_tokens", 0), turn.get("output_tokens", 0))

    def assert_done(self) -> None:
        """Fail unless every scripted turn was requested."""
        if self.turns:
            raise AssertionError(f"{len(self.turns)} scripted turns were never requested")
