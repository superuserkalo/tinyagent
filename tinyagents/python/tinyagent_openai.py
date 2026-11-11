"""OpenAI Responses serialization, streaming, and replay."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import replace
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
    Transport,
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

OPENAI_API = "https://api.openai.com/v1/responses"


class OpenAIProvider:
    def __init__(
        self,
        api_key: str | None = None,
        event_transport: EventTransport | None = None,
        *,
        endpoint: str = OPENAI_API,
        base_url: str | None = None,
        headers: dict[str, str] | None = None,
        extra: JsonObject | None = None,
        timeout: float = 180,
    ) -> None:
        """base_url replaces the API root, headers are added to (or replace) the
        request headers, and extra is merged into every request body."""
        self.api_key = api_key
        self.endpoint = base_url.rstrip("/") + "/responses" if base_url else endpoint
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
            "instructions": instructions,
            "input": _openai_input(history),
            "tools": [tool.definition() for tool in tools],
            "parallel_tool_calls": False,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "stream": True,
        }
        output: dict[int, JsonObject] = {}
        arguments: dict[int, Arguments] = {}
        terminal = False
        usage: object = None
        events: Iterator[JsonObject] | None = None
        try:
            payload.update(self.extra)
            payload.update(extra or {})
            events = iter(self._event_transport(payload, cancellation))
            for event in events:
                cancellation.raise_if_cancelled()
                event_type = string_value(event, "type", required=True)
                if event_type in (
                    "response.output_item.added",
                    "response.output_item.done",
                ):
                    index = index_value(event.get("output_index"))
                    item = copy.deepcopy(object_value(event.get("item"), "output item"))
                    if string_value(item, "type", required=True) == "function_call":
                        value = string_value(item, "arguments")
                        if value:
                            arguments.setdefault(index, Arguments()).set(value)
                    output[index] = item
                elif event_type == "response.output_text.delta":
                    delta = string_value(event, "delta")
                    if delta:
                        yield ProviderEvent("text_delta", delta)
                elif event_type == "response.reasoning_summary_text.delta":
                    delta = string_value(event, "delta")
                    if delta:
                        yield ProviderEvent("reasoning_delta", delta)
                elif event_type == "response.function_call_arguments.delta":
                    index = index_value(event.get("output_index"))
                    arguments.setdefault(index, Arguments()).append(
                        string_value(event, "delta")
                    )
                elif event_type == "response.function_call_arguments.done":
                    index = index_value(event.get("output_index"))
                    arguments.setdefault(index, Arguments()).set(
                        string_value(event, "arguments")
                    )
                elif event_type == "response.completed":
                    response = object_value(event.get("response"), "completed response")
                    if "status" in response and response["status"] != "completed":
                        raise ProviderError("OpenAI response is incomplete")
                    usage = response.get("usage")
                    if "output" in response:
                        items = response["output"]
                        if not isinstance(items, list):
                            raise ProviderError("OpenAI output must be an array")
                        output = {
                            index: copy.deepcopy(object_value(item, "output item"))
                            for index, item in enumerate(items)
                        }
                    terminal = True
                    break
                elif event_type in ("response.failed", "response.incomplete", "error"):
                    raise ProviderError(_provider_error_message(event))
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
        if not terminal:
            raise ProviderError("OpenAI stream ended before response.completed")
        for index, item in output.items():
            if (
                item.get("type") == "function_call"
                and not item.get("arguments")
                and index in arguments
            ):
                item["arguments"] = arguments[index].value()
        turn = _openai_turn([output[index] for index in sorted(output)])
        return replace(turn, input_tokens=token_count(usage, "input_tokens"),
                       output_tokens=token_count(usage, "output_tokens"))

    def _http_events(
        self, payload: JsonObject, cancellation: CancellationToken
    ) -> Iterator[JsonObject]:
        key = self.api_key if self.api_key is not None else os.getenv("OPENAI_API_KEY")
        if not key and "authorization" not in {name.lower() for name in self.headers}:
            raise ProviderError("OPENAI_API_KEY is not set")
        yield from _http_sse_events(
            self.endpoint,
            payload,
            merge_headers({
                **({"Authorization": f"Bearer {key}"} if key else {}),
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            }, self.headers),
            cancellation,
            self.timeout,
        )


class _CompleteTransportProvider:
    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def stream(
        self,
        model: str,
        instructions: str,
        history: list[JsonObject],
        tools: Iterable[Tool],
        cancellation: CancellationToken,
    ) -> Generator[ProviderEvent, None, ProviderTurn]:
        cancellation.raise_if_cancelled()
        output = self._transport(
            {
                "model": model,
                "instructions": instructions,
                "input": _openai_input(history),
                "tools": [tool.definition() for tool in tools],
                "parallel_tool_calls": False,
                "store": False,
                "include": ["reasoning.encrypted_content"],
            }
        )
        cancellation.raise_if_cancelled()
        turn = _openai_turn(copy.deepcopy(output))
        if turn.text:
            yield ProviderEvent("text_delta", turn.text)
        return turn


def _openai_turn(output: list[JsonObject]) -> ProviderTurn:
    calls: list[ToolCall] = []
    ids: set[str] = set()
    text: list[str] = []
    for item in output:
        kind = string_value(item, "type", required=True)
        if kind == "function_call":
            name = string_value(item, "name", required=True)
            call_id = string_value(item, "call_id", required=True)
            if call_id in ids:
                raise ProviderError("duplicate OpenAI tool call")
            ids.add(call_id)
            raw = string_value(item, "arguments")
            if len(raw.encode("utf-8")) > MAX_SSE_BYTES:
                raise ProviderError("tool arguments exceed their byte limit")
            try:
                arguments = parse_object(raw)
            except ProviderError as error:
                raise ProviderError(
                    f"invalid arguments for tool {name}: {error}"
                ) from error
            calls.append(ToolCall(call_id, name, arguments))
        elif kind == "message":
            content = item.get("content")
            if not isinstance(content, list):
                raise ProviderError("OpenAI message is missing content")
            for part in content:
                part = object_value(part, "message content")
                if string_value(part, "type", required=True) == "output_text":
                    text.append(string_value(part, "text"))
    answer = "".join(text)
    return ProviderTurn(
        answer, _assistant_message(output, calls, answer), output, calls
    )


def _openai_input(history: Iterable[JsonObject]) -> list[JsonObject]:
    provider_input: list[JsonObject] = []
    for entry in history:
        role = entry.get("role")
        if role == "user":
            provider_input.append({"role": "user", "content": _openai_content(entry.get("content", ""))})
        elif role == "assistant":
            metadata = entry.get("providerMetadata")
            replay = (
                metadata.get("output")
                if isinstance(metadata, dict)
                and metadata.get("provider") == "openai-responses"
                else None
            )
            if isinstance(replay, list):
                provider_input.extend(item for item in replay if isinstance(item, dict))
                continue
            for block in entry.get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    provider_input.append(
                        {"role": "assistant", "content": block["text"]}
                    )
                elif block.get("type") == "tool_call":
                    provider_input.append(
                        {
                            "type": "function_call",
                            "call_id": block.get("callId"),
                            "name": block.get("name"),
                            "arguments": json.dumps(block.get("arguments", {})),
                        }
                    )
        elif role == "tool_result":
            provider_input.append(
                {
                    "type": "function_call_output",
                    "call_id": entry.get("callId"),
                    "output": _openai_content(entry.get("output", "")),
                }
            )
        else:
            # Accept pre-0.0.1 OpenAI-native history for migration.
            provider_input.append(entry)
    return provider_input


def _openai_content(content: object) -> object:
    """Text stays a string; a list of parts becomes Responses input parts."""
    if not isinstance(content, list):
        return content
    parts: list[JsonObject] = []
    for part in content:
        if part.get("type") == "text":
            parts.append({"type": "input_text", "text": part.get("text", "")})
        elif part.get("type") == "image":
            url = part.get("url") or f"data:{part.get('mediaType')};base64,{part.get('data')}"
            parts.append({"type": "input_image", "image_url": url})
    return parts


def _assistant_message(
    output: list[JsonObject], calls: list[ToolCall], text: str
) -> JsonObject:
    calls_by_id = {call.call_id: call for call in calls}
    content: list[JsonObject] = []
    for item in output:
        if item.get("type") == "reasoning":
            summary = "\n\n".join(
                str(part.get("text", ""))
                for part in item.get("summary", [])
                if isinstance(part, dict)
            )
            content.append(
                {"type": "reasoning", **({"text": summary} if summary else {})}
            )
        elif item.get("type") == "message":
            for part in item.get("content", []):
                if (
                    isinstance(part, dict)
                    and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str)
                ):
                    content.append({"type": "text", "text": part["text"]})
        elif item.get("type") == "function_call":
            call = calls_by_id.get(item.get("call_id"))
            if call:
                content.append(
                    {
                        "type": "tool_call",
                        "callId": call.call_id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                )
    if text and not any(block.get("type") == "text" for block in content):
        content.append({"type": "text", "text": text})
    return {
        "role": "assistant",
        "content": content,
        "providerMetadata": {"provider": "openai-responses", "output": output},
    }


def _provider_error_message(event: JsonObject) -> str:
    if event.get("type") == "error":
        return str(event.get("message") or "OpenAI stream error")
    response = event.get("response")
    if isinstance(response, dict):
        error = response.get("error")
        if isinstance(error, dict):
            return str(
                error.get("message") or error.get("code") or "OpenAI response failed"
            )
        details = response.get("incomplete_details")
        if isinstance(details, dict) and details.get("reason"):
            return f"OpenAI response incomplete: {details['reason']}"
    return f"OpenAI stream failed: {event.get('type')}"
