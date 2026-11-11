"""OpenAI-compatible Chat Completions serialization and streaming.

Most gateways and self-hosted model servers speak this API. It is also the
shortest example of a provider: turn history into a request, read the stream,
yield text as it arrives, and return one ProviderTurn.
"""

from __future__ import annotations

import json
import os
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
from tinyagent_sse import Arguments, http_sse_events, merge_headers, parse_object, token_count

CHAT_API = "https://api.openai.com/v1"


class ChatCompletionsProvider:
    def __init__(
        self,
        api_key: str | None = None,
        event_transport: EventTransport | None = None,
        *,
        base_url: str = CHAT_API,
        headers: dict[str, str] | None = None,
        extra: JsonObject | None = None,
        timeout: float = 180,
    ) -> None:
        """base_url is the API root, without /chat/completions. The key defaults
        to OPENAI_API_KEY, read when a request starts; servers that need none
        accept any value."""
        self.api_key = api_key
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
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
            "messages": ([{"role": "system", "content": instructions}] if instructions else [])
            + _chat_messages(history),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        definitions = [{"type": "function", "function": {"name": tool.name, "description": tool.description,
                                                         "parameters": tool.parameters}} for tool in tools]
        if definitions:
            payload["tools"] = definitions
        text: list[str] = []
        calls: dict[int, JsonObject] = {}       # index -> {"id", "name", "arguments": Arguments}
        finish: str | None = None
        usage: object = None
        events: Iterator[JsonObject] | None = None
        try:
            payload.update(self.extra)
            payload.update(extra or {})
            events = iter(self._event_transport(payload, cancellation))
            for event in events:
                cancellation.raise_if_cancelled()
                if event.get("error"):
                    error = event["error"]
                    raise ProviderError(str(error.get("message") if isinstance(error, dict) else error))
                usage = event.get("usage") or usage  # optional, and often in a chunk of its own
                choices = event.get("choices")
                choice = choices[0] if isinstance(choices, list) and choices else {}
                delta = choice.get("delta") if isinstance(choice, dict) else None
                if not isinstance(delta, dict):
                    delta = {}
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(reasoning, str) and reasoning:
                    yield ProviderEvent("reasoning_delta", reasoning)
                if isinstance(delta.get("content"), str) and delta["content"]:
                    text.append(delta["content"])
                    yield ProviderEvent("text_delta", delta["content"])
                for part in delta.get("tool_calls") or []:
                    # The first fragment names the call; later ones only add argument text.
                    index = part.get("index", 0) if isinstance(part, dict) else None
                    if type(index) is not int or index < 0:
                        raise ProviderError("invalid tool call fragment")
                    call = calls.setdefault(index, {"id": "", "name": "", "arguments": Arguments()})
                    function = part.get("function") if isinstance(part.get("function"), dict) else {}
                    call["id"] = part.get("id") or call["id"]
                    call["name"] = function.get("name") or call["name"]
                    if isinstance(function.get("arguments"), str):
                        call["arguments"].append(function["arguments"])
                if isinstance(choice, dict) and choice.get("finish_reason"):
                    finish = str(choice["finish_reason"])
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
        if finish is None:
            raise ProviderError("chat completion stream ended before a finish reason")
        if finish not in ("stop", "tool_calls", "function_call"):
            raise ProviderError(f"chat completion is incomplete: {finish}")
        tool_calls: list[ToolCall] = []
        for index in sorted(calls):
            call = calls[index]
            if not call["id"] or not call["name"] or any(call["id"] == other.call_id for other in tool_calls):
                raise ProviderError("chat completion tool call is missing or repeats its id or name")
            try:
                arguments = parse_object(call["arguments"].value() or "{}")
            except ProviderError as error:
                raise ProviderError(f"invalid arguments for tool {call['name']}: {error}") from error
            tool_calls.append(ToolCall(call["id"], call["name"], arguments))
        answer = "".join(text)
        content: list[JsonObject] = [{"type": "text", "text": answer}] if answer else []
        content += [{"type": "tool_call", "callId": call.call_id, "name": call.name, "arguments": call.arguments}
                    for call in tool_calls]
        assistant = {"role": "assistant", "content": content}
        return ProviderTurn(answer, assistant, _chat_messages([assistant]), tool_calls,
                            token_count(usage, "prompt_tokens"), token_count(usage, "completion_tokens"))

    def _http_events(self, payload: JsonObject, cancellation: CancellationToken) -> Iterator[JsonObject]:
        key = self.api_key if self.api_key is not None else os.getenv("OPENAI_API_KEY")
        yield from http_sse_events(
            self.endpoint,
            payload,
            merge_headers({
                **({"Authorization": f"Bearer {key}"} if key else {}),
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            }, self.headers),
            cancellation,
            self.timeout,
            done=True,
        )


def _chat_content(content: object) -> object:
    """Text stays a string; a list of parts becomes Chat Completions content parts."""
    if not isinstance(content, list):
        return content
    parts: list[JsonObject] = []
    for part in content:
        if part.get("type") == "text":
            parts.append({"type": "text", "text": part.get("text", "")})
        elif part.get("type") == "image":
            url = part.get("url") or f"data:{part.get('mediaType')};base64,{part.get('data')}"
            parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def _chat_messages(history: Iterable[JsonObject]) -> list[JsonObject]:
    messages: list[JsonObject] = []
    images: list[JsonObject] = []  # from tool results, waiting for the turn's tool messages to end
    for entry in history:
        role = entry.get("role")
        if images and role != "tool_result":
            # A tool message carries text only, and every tool message of a turn must directly
            # follow the assistant message, so the images come after them as one user message.
            messages.append({"role": "user", "content": _chat_content(images)})
            images = []
        if role == "user":
            messages.append({"role": "user", "content": _chat_content(entry.get("content", ""))})
        elif role == "assistant":
            content = entry.get("content")
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            text = "".join(block.get("text", "") for block in blocks
                           if isinstance(block, dict) and block.get("type") == "text")
            calls = [{"id": block.get("callId"), "type": "function",
                      "function": {"name": block.get("name"),
                                   "arguments": json.dumps(block.get("arguments", {}), ensure_ascii=False)}}
                     for block in blocks if isinstance(block, dict) and block.get("type") == "tool_call"]
            if not text and not calls:
                continue  # an empty assistant turn is not a valid message; leave it out
            message: JsonObject = {"role": "assistant", "content": text or None}
            if calls:
                message["tool_calls"] = calls
            messages.append(message)
        elif role == "tool_result":
            output = entry.get("output", "")
            if isinstance(output, list):
                images += [part for part in output if part.get("type") == "image"]
            words = output if isinstance(output, str) else "".join(
                part.get("text", "") for part in output if part.get("type") == "text") or "(see the image)"
            messages.append({"role": "tool", "tool_call_id": entry.get("callId"), "content": words})
        else:
            messages.append(entry)
    if images:
        messages.append({"role": "user", "content": _chat_content(images)})
    return messages
