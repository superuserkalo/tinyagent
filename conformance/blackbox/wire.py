"""The three provider wire formats, seen from the model server's side.

`items()` turns a request body into one provider-neutral list so the
invariants are written once; `encode()` turns a scripted `Turn` into the SSE
events each API would send.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

WIRES = ("openai", "anthropic", "chat")
PATHS = {"/responses": "openai", "/messages": "anthropic", "/chat/completions": "chat"}


@dataclass
class Call:
    command: str = ""                 # bash command; ignored when `raw` is set
    name: str = "bash"
    raw: str | None = None            # literal arguments text, to send invalid JSON
    id: str = ""                      # filled in by the fake provider

    @property
    def arguments(self) -> str:
        return self.raw if self.raw is not None else json.dumps({"command": self.command}, ensure_ascii=False)


@dataclass
class Turn:
    text: str = ""
    calls: list[Call] = field(default_factory=list)
    usage: tuple[int, int] | None = (11, 7)   # (input, output) tokens; None omits usage


def say(text: str, **options: Any) -> Turn:
    return Turn(text=text, **options)


def bash(*commands: str, text: str = "", **options: Any) -> Turn:
    return Turn(text=text, calls=[Call(command) for command in commands], **options)


# ---------------------------------------------------------------- requests

def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content
                       if isinstance(part, dict) and isinstance(part.get("text"), str))
    return ""


def items(wire: str, body: dict) -> list[dict]:
    """The conversation in a request as user / assistant / call / result items."""
    out: list[dict] = []
    if wire == "openai":
        for entry in body.get("input") or []:
            kind = entry.get("type")
            if kind == "function_call":
                out.append({"kind": "call", "id": entry.get("call_id"), "name": entry.get("name"),
                            "arguments": entry.get("arguments")})
            elif kind == "function_call_output":
                out.append({"kind": "result", "id": entry.get("call_id"), "text": _text(entry.get("output")),
                            "raw": entry})
            elif entry.get("role") in ("user", "assistant"):
                out.append({"kind": entry["role"], "text": _text(entry.get("content")), "raw": entry})
    elif wire == "anthropic":
        for message in body.get("messages") or []:
            content = message.get("content")
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            plain = [block for block in blocks if block.get("type") in ("text", "image")]
            if message.get("role") == "user":
                for block in blocks:
                    if block.get("type") == "tool_result":
                        out.append({"kind": "result", "id": block.get("tool_use_id"),
                                    "text": _text(block.get("content")), "error": block.get("is_error"),
                                    "raw": block})
                if plain:
                    out.append({"kind": "user", "text": _text(plain), "raw": message})
            else:
                if plain:
                    out.append({"kind": "assistant", "text": _text(plain), "raw": message})
                for block in blocks:
                    if block.get("type") == "tool_use":
                        out.append({"kind": "call", "id": block.get("id"), "name": block.get("name"),
                                    "arguments": json.dumps(block.get("input"))})
    else:
        for message in body.get("messages") or []:
            role = message.get("role")
            if role == "tool":
                out.append({"kind": "result", "id": message.get("tool_call_id"),
                            "text": _text(message.get("content")), "raw": message})
            elif role == "user":
                out.append({"kind": "user", "text": _text(message.get("content")), "raw": message})
            elif role == "assistant":
                if message.get("content"):
                    out.append({"kind": "assistant", "text": _text(message.get("content")), "raw": message})
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    out.append({"kind": "call", "id": call.get("id"), "name": function.get("name"),
                                "arguments": function.get("arguments")})
    return out


def has_tools(body: dict) -> bool:
    return bool(body.get("tools"))


def problems(wire: str, body: dict) -> list[str]:
    """Why a real provider would reject this request; empty when well-formed."""
    found: list[str] = []
    if body.get("stream") is not True:
        found.append("stream is not true")
    if not isinstance(body.get("model"), str) or not body["model"]:
        found.append("model is missing")
    if wire == "anthropic" and not isinstance(body.get("max_tokens"), int):
        found.append("max_tokens is missing")
    sequence = items(wire, body)
    if not sequence:
        found.append("no conversation items")
    open_calls: list[str] = []
    seen: set[str] = set()
    answered: set[str] = set()
    for item in sequence:
        if item["kind"] == "call":
            if not item["id"] or not item["name"]:
                found.append("tool call without an id or name")
            if item["id"] in seen:
                found.append(f"tool call {item['id']} sent twice")
            seen.add(item["id"])
            open_calls.append(item["id"])
        elif item["kind"] == "result":
            if item["id"] in answered:
                found.append(f"tool result {item['id']} sent twice")
            elif item["id"] not in open_calls:
                found.append(f"tool result {item['id']} without its call")
            else:
                open_calls.remove(item["id"])
            answered.add(item["id"])
        elif item["kind"] == "user" and open_calls:
            found.append(f"tool call {open_calls[0]} without a result")
            open_calls = []
    if open_calls:
        found.append(f"tool call {open_calls[0]} without a result")
    if sequence and sequence[-1]["kind"] not in ("user", "result"):
        found.append("request does not end with a user message or a tool result")
    return found


# --------------------------------------------------------------- responses

def _sse(value: Any, event: str | None = None) -> bytes:
    head = f"event: {event}\n" if event else ""
    return (head + "data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()


def _pieces(text: str, size: int) -> list[str]:
    return [text[index:index + size] for index in range(0, len(text), size)]


def encode(wire: str, turn: Turn, *, model: str = "fake", fragment: int = 0) -> list[bytes]:
    """SSE events for one model turn. `fragment` is the tool-argument piece size (0: two halves)."""
    def argument_pieces(text: str) -> list[str]:
        return _pieces(text, fragment or max(1, (len(text) + 1) // 2))

    events: list[bytes] = []
    if wire == "openai":
        output: list[dict] = []
        if turn.text:
            events.append(_sse({"type": "response.output_item.added", "output_index": 0,
                                "item": {"type": "message", "id": "msg_1", "role": "assistant", "content": []}}))
            events += [_sse({"type": "response.output_text.delta", "output_index": 0, "delta": piece},
                            "response.output_text.delta") for piece in _pieces(turn.text, 6)]
            output.append({"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": turn.text, "annotations": []}]})
            events.append(_sse({"type": "response.output_item.done", "output_index": 0, "item": output[0]}))
        for call in turn.calls:
            index = len(output)
            item = {"type": "function_call", "id": f"fc_{call.id}", "call_id": call.id, "name": call.name}
            events.append(_sse({"type": "response.output_item.added", "output_index": index,
                                "item": {**item, "arguments": ""}}))
            events += [_sse({"type": "response.function_call_arguments.delta", "output_index": index,
                             "delta": piece}) for piece in argument_pieces(call.arguments)]
            events.append(_sse({"type": "response.function_call_arguments.done", "output_index": index,
                                "arguments": call.arguments}))
            output.append({**item, "arguments": call.arguments, "status": "completed"})
            events.append(_sse({"type": "response.output_item.done", "output_index": index, "item": output[-1]}))
        response: dict = {"id": "resp_1", "status": "completed", "model": model, "output": output}
        if turn.usage:
            response["usage"] = {"input_tokens": turn.usage[0], "output_tokens": turn.usage[1],
                                 "total_tokens": sum(turn.usage)}
        events.append(_sse({"type": "response.completed", "response": response}, "response.completed"))
    elif wire == "anthropic":
        usage = {"input_tokens": turn.usage[0], "output_tokens": 0} if turn.usage else {}
        events.append(_sse({"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, **({"usage": usage} if turn.usage else {})}}, "message_start"))
        index = 0
        if turn.text:
            events.append(_sse({"type": "content_block_start", "index": 0,
                                "content_block": {"type": "text", "text": ""}}, "content_block_start"))
            events += [_sse({"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta", "text": piece}}, "content_block_delta")
                       for piece in _pieces(turn.text, 6)]
            events.append(_sse({"type": "content_block_stop", "index": 0}, "content_block_stop"))
            index = 1
        for call in turn.calls:
            events.append(_sse({"type": "content_block_start", "index": index, "content_block": {
                "type": "tool_use", "id": call.id, "name": call.name, "input": {}}}, "content_block_start"))
            events += [_sse({"type": "content_block_delta", "index": index,
                             "delta": {"type": "input_json_delta", "partial_json": piece}}, "content_block_delta")
                       for piece in argument_pieces(call.arguments)]
            events.append(_sse({"type": "content_block_stop", "index": index}, "content_block_stop"))
            index += 1
        events.append(_sse({"type": "message_delta",
                            "delta": {"stop_reason": "tool_use" if turn.calls else "end_turn", "stop_sequence": None},
                            **({"usage": {"output_tokens": turn.usage[1]}} if turn.usage else {})}, "message_delta"))
        events.append(_sse({"type": "message_stop"}, "message_stop"))
    else:
        def chunk(delta: dict, finish: str | None = None) -> bytes:
            return _sse({"id": "chatcmpl_1", "object": "chat.completion.chunk", "model": model,
                         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

        events.append(chunk({"role": "assistant", "content": ""}))
        events += [chunk({"content": piece}) for piece in _pieces(turn.text, 6)]
        for index, call in enumerate(turn.calls):
            events.append(chunk({"tool_calls": [{"index": index, "id": call.id, "type": "function",
                                                 "function": {"name": call.name, "arguments": ""}}]}))
            events += [chunk({"tool_calls": [{"index": index, "function": {"arguments": piece}}]})
                       for piece in argument_pieces(call.arguments)]
        events.append(chunk({}, "tool_calls" if turn.calls else "stop"))
        if turn.usage:
            events.append(_sse({"id": "chatcmpl_1", "object": "chat.completion.chunk", "model": model, "choices": [],
                                "usage": {"prompt_tokens": turn.usage[0], "completion_tokens": turn.usage[1],
                                          "total_tokens": sum(turn.usage)}}))
        events.append(b"data: [DONE]\n\n")
    return events


def oversized(wire: str) -> bytes:
    """One text event larger than the 1 MiB line limit every implementation applies."""
    filler = "x" * 1_200_000
    if wire == "openai":
        return _sse({"type": "response.output_text.delta", "output_index": 0, "delta": filler})
    if wire == "anthropic":
        return _sse({"type": "ping", "padding": filler}, "ping")
    return _sse({"id": "chatcmpl_1", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {"content": filler}, "finish_reason": None}]})


def error_event(wire: str) -> bytes:
    """The in-stream error each API sends when it fails after the response began."""
    if wire == "openai":
        return _sse({"type": "error", "code": "server_error", "message": "injected stream error"}, "error")
    if wire == "anthropic":
        return _sse({"type": "error", "error": {"type": "overloaded_error", "message": "injected stream error"}}, "error")
    return _sse({"error": {"message": "injected stream error", "type": "server_error"}})
