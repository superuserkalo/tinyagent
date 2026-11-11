"""Anthropic Messages serialization, streaming, and replay."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from typing import Generator, Iterable, Iterator

from tinyagent_types import (
    CancellationToken,
    CancelledError,
    EventTransport,
    JsonObject,
    ProviderError,
    ProviderEvent,
    ProviderTurn,
    Tool,
    ToolCall,
)
from tinyagent_sse import (
    Arguments,
    MAX_SSE_BYTES,
    http_sse_events as _http_sse_events,
    merge_headers,
    token_count,
    index_value,
    object_value,
    parse_object,
    string_value,
)

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"


@dataclass
class _Block:
    value: JsonObject
    arguments: Arguments = field(default_factory=Arguments)
    open: bool = True


class AnthropicProvider:
    def __init__(
        self,
        api_key: str | None = None,
        event_transport: EventTransport | None = None,
        max_tokens: int = 8192,
        *,
        endpoint: str = ANTHROPIC_API,
        base_url: str | None = None,
        headers: dict[str, str] | None = None,
        extra: JsonObject | None = None,
        timeout: float = 180,
    ) -> None:
        """base_url replaces the API root, headers are added to (or replace) the
        request headers, and extra is merged into every request body."""
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or max_tokens < 1
        ):
            raise ValueError("max_tokens must be a positive integer")
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.endpoint = base_url.rstrip("/") + "/messages" if base_url else endpoint
        self.headers = dict(headers or {})
        self.extra = dict(extra or {})
        self.timeout = timeout
        self._event_transport = event_transport or self._http_events

    def stream(
        self,
        model: str,
        instructions: str,
        history: list[JsonObject],
        tools: Iterable[Tool],
        cancellation: CancellationToken,
        extra: JsonObject | None = None,
    ) -> Generator[ProviderEvent, None, ProviderTurn]:
        cancellation.raise_if_cancelled()
        payload: JsonObject = {
            "model": model,
            "messages": _anthropic_input(history),
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        if instructions:
            payload["system"] = instructions
        definitions = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters,
            }
            for tool in tools
        ]
        if definitions:
            payload["tools"] = definitions
        blocks: dict[int, _Block] = {}
        message: JsonObject | None = None
        usage: JsonObject = {}
        terminal = False
        events: Iterator[JsonObject] | None = None
        try:
            payload.update(self.extra)
            payload.update(extra or {})
            events = iter(self._event_transport(payload, cancellation))
            for event in events:
                cancellation.raise_if_cancelled()
                event_type = string_value(event, "type", required=True)
                if event_type == "message_start":
                    if message is not None:
                        raise ProviderError("repeated Anthropic message_start")
                    message = copy.deepcopy(
                        object_value(event.get("message"), "message")
                    )
                    if "usage" in message:
                        usage.update(object_value(message["usage"], "usage"))
                elif event_type == "content_block_start":
                    index = index_value(event.get("index"))
                    if message is None or index in blocks:
                        raise ProviderError("invalid Anthropic block order")
                    value = copy.deepcopy(
                        object_value(event.get("content_block"), "content block")
                    )
                    string_value(value, "type", required=True)
                    blocks[index] = _Block(value)
                elif event_type in ("content_block_delta", "content_block_stop"):
                    index = index_value(event.get("index"))
                    block = blocks.get(index)
                    if block is None or not block.open:
                        raise ProviderError(
                            "Anthropic event referenced an unopened block"
                        )
                    if event_type == "content_block_stop":
                        if block.value["type"] == "tool_use":
                            raw = block.arguments.value()
                            if raw:
                                try:
                                    block.value["input"] = parse_object(raw)
                                except ProviderError as error:
                                    raise ProviderError(
                                        f"invalid arguments for tool {block.value.get('name')}: {error}"
                                    ) from error
                            object_value(block.value.get("input"), "tool arguments")
                        block.open = False
                        continue
                    delta = object_value(event.get("delta"), "block delta")
                    kind = string_value(delta, "type", required=True)
                    fields = {
                        "text_delta": ("text", "text"),
                        "thinking_delta": ("thinking", "thinking"),
                        "signature_delta": ("thinking", "signature"),
                        "input_json_delta": ("tool_use", "partial_json"),
                    }
                    if kind not in fields:
                        continue
                    expected, field_name = fields[kind]
                    if block.value["type"] != expected:
                        raise ProviderError("Anthropic delta does not match its block")
                    value = string_value(delta, field_name)
                    if kind == "input_json_delta":
                        block.arguments.append(value)
                    else:
                        previous = (
                            string_value(block.value, field_name)
                            if field_name in block.value
                            else ""
                        )
                        block.value[field_name] = previous + value
                        if kind == "text_delta" and value:
                            yield ProviderEvent("text_delta", value)
                        elif kind == "thinking_delta" and value:
                            yield ProviderEvent("reasoning_delta", value)
                elif event_type == "message_delta":
                    if message is None:
                        raise ProviderError("Anthropic delta preceded message_start")
                    message.update(
                        copy.deepcopy(object_value(event.get("delta"), "message delta"))
                    )
                    if "usage" in event:
                        usage.update(object_value(event["usage"], "usage delta"))
                elif event_type == "message_stop":
                    terminal = True
                    break
                elif event_type == "error":
                    error = object_value(event.get("error"), "stream error")
                    raise ProviderError(
                        str(
                            error.get("message")
                            or error.get("type")
                            or "Anthropic stream error"
                        )
                    )
        except CancelledError:
            raise
        except Exception as error:
            cancellation.raise_if_cancelled()
            if isinstance(error, ProviderError):
                raise
            raise ProviderError(str(error)) from error
        finally:
            close = getattr(events, "close", None)
            if close is not None:
                close()
        cancellation.raise_if_cancelled()
        if not terminal or message is None:
            raise ProviderError("Anthropic stream ended before a complete message")
        reason = string_value(message, "stop_reason", required=True)
        if reason not in ("end_turn", "tool_use", "stop_sequence", "refusal"):
            raise ProviderError(f"Anthropic response is incomplete: {reason}")
        output: list[JsonObject] = []
        calls: list[ToolCall] = []
        ids: set[str] = set()
        text: list[str] = []
        for index in sorted(blocks):
            block = blocks[index]
            if block.open:
                raise ProviderError(
                    "Anthropic stream ended with an unfinished content block"
                )
            value = block.value
            if value["type"] == "tool_use":
                call_id = string_value(value, "id", required=True)
                name = string_value(value, "name", required=True)
                arguments = object_value(value.get("input"), "tool arguments")
                if call_id in ids:
                    raise ProviderError("duplicate Anthropic tool call")
                ids.add(call_id)
                if (
                    len(json.dumps(arguments, ensure_ascii=False).encode("utf-8"))
                    > MAX_SSE_BYTES
                ):
                    raise ProviderError("tool arguments exceed their byte limit")
                calls.append(ToolCall(call_id, name, arguments))
            elif value["type"] == "text":
                text.append(string_value(value, "text"))
            output.append(value)
        replay = {**message, "content": output, "usage": usage}
        return ProviderTurn(
            "".join(text), _anthropic_assistant(output, calls, replay), output, calls,
            token_count(usage, "input_tokens"), token_count(usage, "output_tokens"),
        )

    def _http_events(
        self, payload: JsonObject, cancellation: CancellationToken
    ) -> Iterator[JsonObject]:
        key = (
            self.api_key if self.api_key is not None else os.getenv("ANTHROPIC_API_KEY")
        )
        if not key and not {"x-api-key", "authorization"} & {name.lower() for name in self.headers}:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        yield from _http_sse_events(
            self.endpoint,
            payload,
            merge_headers({
                **({"x-api-key": key} if key else {}),
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            }, self.headers),
            cancellation,
            self.timeout,
        )


def _anthropic_input(history: Iterable[JsonObject]) -> list[JsonObject]:
    messages: list[JsonObject] = []
    for entry in history:
        role = entry.get("role")
        if role == "user":
            messages.append({"role": "user", "content": _anthropic_content(entry.get("content", ""))})
        elif role == "assistant":
            metadata = entry.get("providerMetadata")
            replay = (
                metadata.get("message")
                if isinstance(metadata, dict)
                and metadata.get("provider") == "anthropic-messages"
                else None
            )
            content = replay.get("content") if isinstance(replay, dict) else None
            if isinstance(content, list):
                if content:  # an empty assistant turn is not a valid message; leave it out
                    messages.append({"role": "assistant", "content": content})
                continue
            blocks: list[JsonObject] = []
            for block in entry.get("content", []):
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text" and isinstance(block.get("text"), str):
                    blocks.append({"type": "text", "text": block["text"]})
                elif block_type == "tool_call":
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": block.get("callId"),
                            "name": block.get("name"),
                            "input": block.get("arguments", {}),
                        }
                    )
                elif block_type in ("tool_use", "thinking", "redacted_thinking"):
                    blocks.append(block)
            if blocks:
                messages.append({"role": "assistant", "content": blocks})
        elif role == "tool_result":
            result: JsonObject = {
                "type": "tool_result",
                "tool_use_id": entry.get("callId"),
                "content": _anthropic_content(entry.get("output", "")),
                "is_error": bool(entry.get("isError", False)),
            }
            previous_content = (
                messages[-1].get("content")
                if messages and messages[-1].get("role") == "user"
                else None
            )
            if isinstance(previous_content, list) and all(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in previous_content
            ):
                previous_content.append(result)
            else:
                messages.append({"role": "user", "content": [result]})
        else:
            # Accept Anthropic-native messages for migration.
            messages.append(entry)
    return messages


def _anthropic_content(content: object) -> object:
    """Text stays a string; a list of parts becomes Messages content blocks."""
    if not isinstance(content, list):
        return content
    blocks: list[JsonObject] = []
    for part in content:
        if part.get("type") == "text":
            blocks.append({"type": "text", "text": part.get("text", "")})
        elif part.get("type") == "image":
            source = ({"type": "url", "url": part["url"]} if part.get("url") else
                      {"type": "base64", "media_type": part.get("mediaType"), "data": part.get("data")})
            blocks.append({"type": "image", "source": source})
    return blocks


def _anthropic_assistant(
    output: list[JsonObject],
    calls: list[ToolCall],
    replay_message: JsonObject,
) -> JsonObject:
    calls_by_id = {call.call_id: call for call in calls}
    content: list[JsonObject] = []
    for block in output:
        block_type = block.get("type")
        if block_type == "text" and isinstance(block.get("text"), str):
            content.append({"type": "text", "text": block["text"]})
        elif block_type in ("thinking", "redacted_thinking"):
            reasoning = block.get("thinking")
            content.append(
                {
                    "type": "reasoning",
                    **({"text": reasoning} if isinstance(reasoning, str) else {}),
                }
            )
        elif block_type == "tool_use":
            call = calls_by_id.get(block.get("id"))
            if call:
                content.append(
                    {
                        "type": "tool_call",
                        "callId": call.call_id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                )
    return {
        "role": "assistant",
        "content": content,
        "providerMetadata": {
            "provider": "anthropic-messages",
            "message": replay_message,
        },
    }
