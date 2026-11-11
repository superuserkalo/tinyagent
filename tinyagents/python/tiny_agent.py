"""TinyAgent SDK and copyable streaming coding-agent CLI."""

from __future__ import annotations

import argparse
import copy
import json
import os
import queue
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Generator, Iterable, Mapping

from tinyagent_types import (
    AgentEvent,
    CancellationToken,
    CancelledError,
    EventHandler,
    EventTransport,
    HookError,
    JsonObject,
    ToolDenied,
    LimitExceededError,
    OutputMissingError,
    Pause,
    Provider,
    ProviderError,
    ProviderEvent,
    ProviderTurn,
    RepeatedToolErrorsError,
    RunResult,
    Tool,
    ToolCall,
    ToolContext,
    ToolError,
    ToolOutputLimitError,
    Transport,
    check_run_options,
    clean,
    content_problem,
    pause,
    GRACE_SECONDS,
    PROTOCOL_VERSION,
    MAX_JSONL_LINE_BYTES,
    MAX_HTTP_BODY_BYTES,
    MAX_QUEUED_OUTPUT_BYTES,
    MAX_TOOL_OUTPUT_BYTES,
)
from tinyagent_http import AgentServer, serve
from tinyagent_openai import OpenAIProvider, _CompleteTransportProvider
from tinyagent_anthropic import AnthropicProvider
from tinyagent_chat import ChatCompletionsProvider
from tinyagent_testing import ScriptedProvider
from tinyagent_rpc import RpcSession, run_rpc
from tinyagent_session import (Session, ContextBudget, NothingToResumeError,
                               RecoveryRequiredError, ResumeRequiredError)
from tinyagent_storage import (FileStorage, MemoryStorage, SessionBusyError, SessionCorruptionError,
                               SessionFailedError, SessionFencedError, Storage, StorageError, check_storage)

__version__ = "0.2.0"

MODEL = "gpt-5.6"
INSTRUCTIONS = """You are TinyAgent, a coding agent in the current directory.
Inspect before changing files. Use bash for shell and file work. Each call is a fresh process."""
OUTPUT_TOOL = "final_output"
RETRY_ATTEMPTS = 4
RETRY_AFTER_MAX = 30.0
BASH_KEEP_BYTES = 15_000
BASH_READER_GRACE_SECONDS = 1.0
NEVER_AUTO_APPROVED = ";&|$<>()`\\\r\n"  # --allow never approves a command containing one of these
DENIED_WORDS = frozenset({
    "rm",
    "rmdir",
    "unlink",
    "shred",
    "truncate",
    "dd",
    "mkfs",
    "wipefs",
    "fdisk",
    "parted",
    "sudo",
    "doas",
    "shutdown",
    "reboot",
    "poweroff",
    "halt",
    "kill",
    "pkill",
    "killall",
})


class Agent:
    """A model, instructions, a provider and tools. It holds no conversation.

    max_steps, max_seconds and max_tokens bound one run. before_tool and
    after_tool see every tool call. trace adds model_request_start,
    model_request_end and reasoning_delta events. new_id and sleep are
    injectable so a host or a test controls IDs and retry waits.
    """

    def __init__(
        self,
        *,
        model: str,
        instructions: str = "",
        tools: Iterable[Tool] = (),
        api_key: str | None = None,
        provider: Provider | None = None,
        transport: Transport | None = None,
        max_steps: int | None = None,
        max_seconds: float | None = None,
        max_tokens: int | None = None,
        before_tool: Callable[[ToolCall, ToolContext], Any] | None = None,
        after_tool: Callable[[ToolCall, JsonObject, ToolContext], Any] | None = None,
        trace: bool = False,
        new_id: Callable[[str], str] | None = None,
        sleep: Callable[[float, CancellationToken], Any] | None = None,
    ) -> None:
        if provider is not None and transport is not None:
            raise ValueError("provide either provider or transport, not both")
        check_run_options(None, {"max_steps": max_steps, "max_seconds": max_seconds, "max_tokens": max_tokens})
        self.model = model
        self.instructions = instructions
        tool_list = list(tools)
        self.tools = {tool.name: tool for tool in tool_list}
        if len(self.tools) != len(tool_list):
            raise ValueError("tool names must be unique")
        self.provider = provider or (
            _CompleteTransportProvider(transport)
            if transport
            else OpenAIProvider(api_key)
        )
        self.max_steps = max_steps
        self.max_seconds = max_seconds
        self.max_tokens = max_tokens
        self.before_tool = before_tool
        self.after_tool = after_tool
        self.trace = trace
        # new_id("s") names a session and new_id("r") a run.
        self.new_id = new_id or (lambda kind: f"{kind}_{secrets.token_hex(16 if kind == 's' else 12)}")
        self.sleep = sleep or (lambda seconds, cancellation: cancellation.wait(seconds))

    def stream(
        self,
        prompt: str | list[JsonObject],
        history: Iterable[JsonObject] = (),
        *,
        cancellation: CancellationToken | None = None,
        run_id: str | None = None,
        **options: Any,
    ) -> Generator[AgentEvent, None, RunResult]:
        """Run without a Session.

        options: context, instructions, model, extra, output_schema, trace,
        max_steps, max_seconds, max_tokens.
        """
        check_run_options(cancellation, options)
        problem = content_problem(prompt)
        if problem:
            raise ValueError(f"prompt {problem}")
        transcript = clean(list(history))
        transcript.append({"role": "user", "content": clean(prompt)})
        return self._loop(transcript, cancellation=cancellation,
                          run_id=run_id or self.new_id("r"), **options)

    def _loop(
        self,
        transcript: list[JsonObject],
        *,
        run_id: str,
        cancellation: CancellationToken | None = None,
        record: Callable[[JsonObject], int] | None = None,
        prepare: Callable[[list[JsonObject]], Generator[JsonObject, None, list[JsonObject]]] | None = None,
        scope: str | None = None,
        start: JsonObject | None = None,
        turn: ProviderTurn | None = None,
        pending: JsonObject | None = None,
        context: Any = None,
        instructions: str | None = None,
        model: str | None = None,
        extra: JsonObject | None = None,
        output_schema: JsonObject | None = None,
        trace: bool | None = None,
        max_steps: int | None = None,
        max_seconds: float | None = None,
        max_tokens: int | None = None,
        value: Any = None,
    ) -> Generator[AgentEvent, None, RunResult]:
        """The one agent loop: ask the model, run its tool calls, repeat.

        A Session passes record (append a journal record, return its sequence),
        prepare (compaction) and scope (its ID). To resume it also passes the
        stored assistant turn to continue and the pending tool call, if any.
        """
        token = CancellationToken()
        tools = dict(self.tools)
        trace = self.trace if trace is None else trace
        limits = (self.max_steps if max_steps is None else max_steps,
                  self.max_seconds if max_seconds is None else max_seconds,
                  self.max_tokens if max_tokens is None else max_tokens)
        if output_schema is not None:
            if output_schema.get("type") != "object":
                raise ValueError("output_schema must describe a JSON object")
            if OUTPUT_TOOL in tools:
                raise ValueError(f"{OUTPUT_TOOL} is reserved when output_schema is set")
            tools[OUTPUT_TOOL] = Tool(OUTPUT_TOOL, "Give the final answer. Call this exactly once, when done.",
                                      output_schema, lambda arguments: "accepted")
        owner = cancellation or token  # names this run to code that asks "am I inside it?"

        def save(entry: JsonObject) -> int:
            cursor = record(entry) if record else 0
            token.raise_if_cancelled()  # a cancel that landed during the write counts
            return cursor

        usage = {"inputTokens": 0, "outputTokens": 0, "requests": 0}
        began = time.monotonic()
        steps = 0
        sequence = 0
        error_turns = 0
        nudged = False
        terminal = False

        def event(event_type: str, cursor: int = 0, **data: Any) -> AgentEvent:
            nonlocal sequence
            sequence += 1
            if cursor:
                data["cursor"] = cursor
            return AgentEvent(event_type, run_id, sequence, data)

        def finish(turn: ProviderTurn, value: Any = None) -> RunResult:
            nonlocal terminal
            terminal = True
            return RunResult(turn.text, transcript, turn.output, dict(usage), value)

        remove_parent = (
            cancellation.on_cancel(token.cancel) if cancellation else lambda: None
        )
        try:
            yield event("agent_start", **(start or {}))
            while True:
                token.raise_if_cancelled()
                if turn is None:
                    limit = self._limit(limits, steps, usage, began)
                    if limit:
                        raise LimitExceededError(limit)
                    if prepare:
                        preparation = prepare(transcript)
                        try:
                            while True:
                                try:
                                    progress = next(preparation)
                                except StopIteration as completed:
                                    transcript = completed.value
                                    break
                                if progress["type"] == "usage":
                                    # Compaction's summary request belongs to this run's usage.
                                    for key in usage:
                                        usage[key] += progress[key]
                                    continue
                                yield event(progress.pop("type"), progress.pop("cursor", 0), **progress)
                        finally:
                            preparation.close()
                        token.raise_if_cancelled()
                    steps += 1
                    request = self._request(model or self.model,
                                            self.instructions if instructions is None else instructions,
                                            transcript, tools.values(), token, extra, owner)
                    try:
                        while True:
                            try:
                                kind, data = next(request)
                            except StopIteration as completed:
                                turn = completed.value
                                break
                            if kind == "model_request_end" and "usage" in data:
                                usage["requests"] += 1
                                usage["inputTokens"] += data["usage"]["inputTokens"]
                                usage["outputTokens"] += data["usage"]["outputTokens"]
                            if trace or kind == "text_delta":
                                yield event(kind, **data)
                    except (GeneratorExit, KeyboardInterrupt):
                        token.cancel()
                        raise
                    finally:
                        request.close()
                    token.raise_if_cancelled()
                    transcript.append(turn.assistant)
                    save({"type": "item", "item": turn.assistant})
                if not turn.tool_calls and value is not None:
                    # Resumed after final_output was accepted: the stored value completes the run.
                    result = finish(turn, value)
                    yield event("agent_end", status="completed", text=turn.text, value=value, usage=dict(usage))
                    token.raise_if_cancelled()
                    return result
                if not turn.tool_calls:
                    if output_schema is None:
                        result = finish(turn)
                        yield event("agent_end", status="completed", text=turn.text, usage=dict(usage))
                        token.raise_if_cancelled()
                        return result
                    if nudged:
                        raise OutputMissingError(f"the model did not call {OUTPUT_TOOL}")
                    nudged = True
                    reminder = {"role": "user", "content": f"Call the {OUTPUT_TOOL} tool with your final answer."}
                    transcript.append(reminder)
                    save({"type": "item", "item": reminder})
                    turn = None
                    continue
                all_failed = True
                for call in turn.tool_calls:
                    tool = tools.get(call.name)
                    held = pending if pending and pending["callId"] == call.call_id else None
                    pending = None
                    if value is not None:
                        # Calls made alongside final_output still get a result, so history stays valid.
                        held = {"output": "not run: the run finished", "isError": True}
                    cursor = held["sequence"] if held and "sequence" in held else save({
                        "type": "tool_intent", "callId": call.call_id, "toolName": call.name,
                        "arguments": call.arguments, "idempotent": bool(tool and tool.idempotent)})
                    execution_id = f"{scope}:{cursor}" if cursor else call.call_id
                    if held and "sequence" in held and not held.get("interrupted"):
                        # Written before a call is run again or answered, so a writer that lost the
                        # session stops here, and so replay shows the second start.
                        cursor = save({"type": "tool_resumed" if "output" in held else "tool_rerun",
                                       "callId": call.call_id})
                    call_token = CancellationToken()
                    remove_call = token.on_cancel(call_token.cancel)
                    tool_context = ToolContext(call_token, execution_id, context)
                    is_error = True
                    failure = None  # raised after the call has its end event
                    denied = False
                    mark = ({} if not held or "sequence" not in held else {"interrupted": True}
                            if held.get("interrupted") else {"resumed": True} if "output" in held else {"rerun": True})
                    try:
                        yield event("tool_execution_start", cursor, toolCallId=call.call_id,
                                    toolName=call.name, arguments=call.arguments, executionId=execution_id, **mark)
                        if held and "output" in held:
                            # An interrupted, paused or skipped call: its result was decided outside the loop.
                            output = held["output"]
                            if not held["isError"]:
                                _check_tool_output(output)
                            is_error = held["isError"]
                        else:
                            token.raise_if_cancelled()
                            returned = self._execute(tool, call, tool_context, token, owner)
                            token.raise_if_cancelled()
                            if isinstance(returned, Pause):
                                payload = clean(returned.payload)
                                stopped = {"toolCallId": call.call_id, "toolName": call.name, "payload": payload}
                                cursor = save({"type": "tool_paused", "callId": call.call_id,
                                               "toolName": call.name, "payload": payload})
                                # The started call ends, marked paused, before the run does.
                                yield event("tool_execution_end", cursor, toolCallId=call.call_id,
                                            toolName=call.name, output="", isError=False, paused=True)
                                cursor = record({"type": "run_paused", "pause": stopped,
                                                 "usage": dict(usage)}) if record else 0
                                terminal = True
                                yield event("agent_end", cursor, status="paused", pause=stopped, usage=dict(usage))
                                return RunResult("", transcript, None, dict(usage), status="paused", pause=stopped)
                            output = _tool_output(returned)
                            is_error = False
                    except CancelledError as error:
                        if token.cancelled:
                            # Only the run's own cancellation aborts the run.
                            yield event("tool_execution_end", toolCallId=call.call_id, toolName=call.name,
                                        output="run aborted", isError=True)
                            raise
                        output = (f"timed out after {tool.timeout_seconds:g} s" if tool_context.timed_out
                                  else clean(f"{type(error).__name__}: {error}"))
                    except HookError as error:
                        # The call still gets a result, so nothing is left pending; then the run fails.
                        failure, output = error, str(error)
                    except ToolError as error:
                        denied = isinstance(error, ToolDenied)
                        output = clean(str(error))
                        if tool_context.timed_out:
                            output += f"[timed out after {tool.timeout_seconds:g} s]"
                    except Exception as error:
                        output = clean(f"{type(error).__name__}: {error}")
                        if tool_context.timed_out and not token.cancelled:
                            output = f"timed out after {tool.timeout_seconds:g} s"
                    finally:
                        remove_call()
                    if is_error and len(output.encode("utf-8")) > MAX_TOOL_OUTPUT_BYTES:
                        output = f"ToolOutputLimitError: tool error exceeds {MAX_TOOL_OUTPUT_BYTES} UTF-8 bytes"
                    observation = {"role": "tool_result", "callId": call.call_id, "toolName": call.name,
                                   "output": output, "isError": is_error}
                    # after_tool sees every settled call, failed ones too; not a denied or interrupted one.
                    if self.after_tool and not failure and not denied and (
                            not held or "output" not in held or held.get("answered")):
                        try:
                            observation.update(self._after(call, observation, tool_context, token, owner))
                        except HookError as error:
                            failure = error
                            observation.update(output=str(error), isError=True)
                    final = (call.name == OUTPUT_TOOL and output_schema is not None and not observation["isError"]
                             and value is None)
                    try:
                        # The accepted value is stored with the result, so it survives a crash.
                        cursor = save({"type": "tool_result", "item": observation,
                                       **({"value": call.arguments} if final else {})})
                    except Exception as error:
                        # The result could not be recorded. The call still ends for whoever is watching.
                        failure, cursor = error, 0
                    yield event("tool_execution_end", cursor, toolCallId=call.call_id, toolName=call.name,
                                output=observation["output"], isError=observation["isError"])
                    transcript.append(observation)
                    if failure:
                        raise failure
                    all_failed = all_failed and observation["isError"]
                    if final:
                        value = call.arguments
                if value is not None:
                    result = finish(turn, value)
                    yield event("agent_end", status="completed", text=turn.text, value=value, usage=dict(usage))
                    token.raise_if_cancelled()
                    return result
                turn = None
                error_turns = error_turns + 1 if all_failed else 0
                if error_turns == 3:
                    raise RepeatedToolErrorsError("every tool call failed in three consecutive turns")
        except Exception as error:
            if terminal:
                raise
            if token.cancelled:
                yield event("agent_end", status="aborted", usage=dict(usage))
                if isinstance(error, CancelledError):
                    raise
                raise CancelledError("run aborted") from error
            failure = {"code": getattr(error, "code", "provider_error"), "message": clean(str(error)),
                       "retryable": bool(getattr(error, "retryable", False))}
            if isinstance(error, LimitExceededError):
                failure["limit"] = error.limit
            yield event("agent_end", status="failed", error=failure, usage=dict(usage))
            raise
        finally:
            remove_parent()
            token.cancel()

    @staticmethod
    def _limit(limits: tuple[Any, Any, Any], steps: int, usage: JsonObject, began: float) -> str | None:
        """Checked before each model request. A retried request is one step."""
        max_steps, max_seconds, max_tokens = limits
        if max_steps is not None and steps >= max_steps:
            return "steps"
        if max_seconds is not None and time.monotonic() - began >= max_seconds:
            return "seconds"
        if max_tokens is not None and usage["inputTokens"] + usage["outputTokens"] >= max_tokens:
            return "tokens"
        return None

    def _request(self, model: str, instructions: str, transcript: list[JsonObject], tools: Iterable[Tool],
                 token: CancellationToken, extra: JsonObject | None, owner: Any,
                 ) -> Generator[tuple[str, JsonObject], None, ProviderTurn]:
        """One model request. A failure before anything arrived is tried up to four times.

        The provider runs in a thread of its own and hands over one item at a
        time, so a provider that never returns cannot hold a cancelled run.
        """
        options = {} if extra is None else {"extra": extra}
        tool_list = list(tools)
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            items: queue.SimpleQueue = queue.SimpleQueue()
            wanted = threading.Semaphore(0)  # the provider advances only when the loop asks
            abandoned = threading.Event()

            def pump() -> None:
                stream = None
                try:
                    stream = self.provider.stream(model, instructions, transcript, tool_list, token, **options)
                    while True:
                        while not wanted.acquire(timeout=0.05):
                            if abandoned.is_set():
                                return
                        try:
                            items.put(("event", next(stream)))
                        except StopIteration as completed:
                            return items.put(("turn", completed.value))
                except BaseException as error:
                    items.put(("error", error))
                finally:
                    close = getattr(stream, "close", None)
                    if close:
                        close()

            worker = threading.Thread(target=pump, name="tinyagent-provider", daemon=True)
            worker.tinyagent_owner = owner
            received = False
            yield "model_request_start", {"attempt": attempt}
            worker.start()
            try:
                while True:
                    wanted.release()
                    kind, item = _take(items, token)
                    if kind == "error":
                        raise item
                    if kind == "turn":
                        turn = _checked_turn(item)
                        yield "model_request_end", {"usage": {"inputTokens": turn.input_tokens,
                                                              "outputTokens": turn.output_tokens}}
                        return turn
                    received = True
                    token.raise_if_cancelled()
                    if getattr(item, "type", None) in ("text_delta", "reasoning_delta") and isinstance(item.delta, str):
                        yield item.type, {"delta": clean(item.delta)}
            except ProviderError as error:
                retry = (not received and attempt < RETRY_ATTEMPTS and error.retryable
                         and not token.cancelled)
                yield "model_request_end", {"error": {"code": "provider_error", "message": clean(str(error))},
                                            "willRetry": retry}
                if not retry:
                    raise
                delay = 2.0 ** (attempt - 1) if error.retry_after is None else min(error.retry_after, RETRY_AFTER_MAX)
            except Exception as error:
                if token.cancelled:
                    yield "model_request_end", {"error": {"code": "aborted", "message": "run aborted"},
                                                "willRetry": False}
                    raise
                yield "model_request_end", {"error": {"code": "provider_error", "message": clean(str(error))},
                                            "willRetry": False}
                if isinstance(error, CancelledError):
                    # The provider gave up for reasons of its own. That is its error, not a cancelled run.
                    raise ProviderError(f"provider stopped: {error}") from error
                raise
            finally:
                abandoned.set()
                worker.join(GRACE_SECONDS)  # a provider that honours cancellation has cleaned up by now
            self.sleep(delay, token)
            token.raise_if_cancelled()
        raise AssertionError("unreachable")

    @staticmethod
    def _guarded(work: Callable[[], Any], token: CancellationToken, owner: Any,
                 timeout: float | None = None, context: ToolContext | None = None) -> Any:
        """Call host code (a handler or a hook) in a thread, so it cannot hold the run.

        The wait races the run's cancellation and the tool's timeout. After
        either, the code has one second to settle; then it is left behind.
        """
        outcome: list[tuple[Any, BaseException | None]] = []
        done = threading.Event()

        def body() -> None:
            try:
                outcome.append((work(), None))
            except BaseException as error:
                outcome.append((None, error))
            finally:
                done.set()

        worker = threading.Thread(target=body, name="tinyagent-host", daemon=True)
        worker.tinyagent_owner = owner
        remove = token.on_cancel(done.set)
        worker.start()
        try:
            done.wait(timeout)
        finally:
            remove()
        if not outcome:
            if context is not None and not token.cancelled:
                context.timed_out = True
                context.cancellation.cancel()
            worker.join(GRACE_SECONDS)  # a cooperative handler returns what it has
        if not outcome:
            raise CancelledError("run aborted" if token.cancelled else "timed out")
        result, error = outcome[0]
        if error is not None:
            raise error
        return result

    def _execute(self, tool: Tool | None, call: ToolCall, context: ToolContext,
                 token: CancellationToken, owner: Any) -> Any:
        """Validate, ask before_tool, then run the handler under its timeout."""
        if tool is None:
            raise ToolError(f"Unknown tool: {call.name}")
        tool.validate(call.arguments)
        # Handlers and hooks get copies: editing them cannot change what was journaled or is sent back.
        arguments = copy.deepcopy(call.arguments)
        if self.before_tool:
            seen = ToolCall(call.call_id, call.name, copy.deepcopy(arguments))
            try:
                decision = self._guarded(lambda: self.before_tool(seen, context), token, owner)
            except Exception as error:
                if token.cancelled:
                    raise CancelledError("run aborted") from error
                raise HookError(f"before_tool failed: {type(error).__name__}: {error}") from error
            if isinstance(decision, Pause):
                return decision
            if decision is False or isinstance(decision, str):
                raise ToolDenied(clean(decision) or "Tool call denied")
            if isinstance(decision, dict):
                arguments = clean(decision)
            elif decision is not None and decision is not True:
                raise HookError("before_tool must return None, True, False, a reason, new arguments, or pause()")
        return self._guarded(lambda: tool.execute(arguments, context), token, owner,
                             tool.timeout_seconds, context)

    def _after(self, call: ToolCall, observation: JsonObject, context: ToolContext,
               token: CancellationToken, owner: Any) -> JsonObject:
        seen = ToolCall(call.call_id, call.name, copy.deepcopy(call.arguments))
        result = copy.deepcopy({"output": observation["output"], "isError": observation["isError"]})
        try:
            changed = self._guarded(lambda: self.after_tool(seen, result, context), token, owner)
        except Exception as error:
            if token.cancelled:
                raise CancelledError("run aborted") from error
            raise HookError(f"after_tool failed: {type(error).__name__}: {error}") from error
        if changed is None:
            return {}
        if isinstance(changed, str):
            changed = {"output": changed, "isError": observation["isError"]}
        if isinstance(changed, dict) and "output" in changed:
            try:
                # A replaced result is held to the same rules as a tool's own.
                return {"output": _tool_output(changed["output"]), "isError": bool(changed.get("isError", False))}
            except Exception as error:
                raise HookError(f"after_tool returned an unusable output: {error}") from error
        raise HookError("after_tool must return None, a string, or {'output': ..., 'isError': ...}")

    def run(
        self,
        prompt: str | list[JsonObject],
        history: Iterable[JsonObject] = (),
        *,
        cancellation: CancellationToken | None = None,
        on_event: EventHandler | None = None,
        run_id: str | None = None,
        **options: Any,
    ) -> RunResult:
        events = self.stream(prompt, history, cancellation=cancellation, run_id=run_id, **options)
        try:
            while True:
                try:
                    current = next(events)
                except StopIteration as completed:
                    return completed.value
                if on_event:
                    on_event(current)
        finally:
            events.close()


def denied_reason(command: str) -> str | None:
    normalized = command.lower().replace("\\", "").replace('"', "").replace("'", "")
    words = set(re.findall(r"[a-z0-9_.-]+", normalized))
    blocked = sorted(words & DENIED_WORDS)
    if any(word.startswith("mkfs.") for word in words):
        blocked.append("mkfs")
    if blocked:
        return f"'{blocked[0]}' is denied"
    if "find" in words and "-delete" in words:
        return "'find -delete' is denied"
    if "git" in words and (
        "clean" in words
        or "restore" in words
        or ("reset" in words and "--hard" in words)
        or ("checkout" in words and "--" in words)
    ):
        return "destructive git command is denied"
    if ":(){:|:&};:" in "".join(normalized.split()):
        return "fork bomb is denied"
    return None


class _Capture:
    """The first and last BASH_KEEP_BYTES of one stream, and how much it produced."""

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self.lock = threading.Lock()

    def add(self, chunk: bytes) -> None:
        with self.lock:
            self.total += len(chunk)
            room = BASH_KEEP_BYTES - len(self.head)
            self.head.extend(chunk[:room])
            self.tail.extend(chunk[max(room, 0):])
            del self.tail[:-BASH_KEEP_BYTES]

    def text(self) -> str:
        with self.lock:
            head, tail, total = bytes(self.head), bytes(self.tail), self.total
        whole = (head + tail).decode("utf-8", errors="replace")
        if total <= 2 * BASH_KEEP_BYTES and len(whole.encode()) <= 2 * BASH_KEEP_BYTES:
            return whole
        # Decode first, cut second: bytes that are not UTF-8 become U+FFFD, which is three bytes,
        # so the cut is made on the decoded text and always lands between characters.
        start, end = _fit(head, False), _fit(tail, True)
        elided = total - len(head[:start]) - len(tail[end:])
        return (head[:start].decode("utf-8", errors="replace") + f"\n[... {elided} bytes elided ...]\n"
                + tail[end:].decode("utf-8", errors="replace"))


def _fit(data: bytes, from_end: bool) -> int:
    """Where to cut data so that the kept part, decoded, is at most BASH_KEEP_BYTES of whole characters."""
    def kept(cut: int) -> bytes:
        return data[cut:] if from_end else data[:cut]

    def fits(cut: int) -> bool:
        text = kept(cut).decode("utf-8", errors="replace")
        # A replacement character at the cut edge means the cut split a character.
        edge = text[:1] if from_end else text[-1:]
        return len(text.encode()) <= BASH_KEEP_BYTES and (edge != "�" or cut in (0, len(data))
                                                         or not _splits(data, cut))

    cuts = range(0, len(data) + 1) if from_end else range(len(data), -1, -1)
    return next(cut for cut in cuts if fits(cut))


def _splits(data: bytes, cut: int) -> bool:
    """True when cut falls inside a UTF-8 character."""
    return 0 < cut < len(data) and data[cut] & 0xC0 == 0x80


def run_bash(command: str, cancellation: CancellationToken | ToolContext | None = None, *,
             cwd: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """Run one command in its own process group and return its exit code and output.

    Each of stdout and stderr keeps its first and last 15,000 bytes. Cancelling
    kills the whole group; under a tool timeout the captured output comes back
    as a ToolError instead.
    """
    reason = denied_reason(command)
    if reason:
        raise ToolError(f"exit_code: 126\nstdout:\n\nstderr:\nblocked: {reason}\n")
    token = cancellation or CancellationToken()
    token.raise_if_cancelled()
    process = subprocess.Popen(
        ["bash", "-c", command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=None if env is None else {**os.environ, **env},
        start_new_session=os.name == "posix",
    )
    stop_errors: list[OSError] = []

    stopping = threading.Lock()

    def stop() -> None:
        with stopping:
            # Descendants can still own the pipes after their shell has exited.
            if os.name != "posix" and process.poll() is not None:
                return
            try:
                if os.name == "posix":
                    # One SIGKILL to the group is not always enough: macOS refuses to signal a member
                    # that is in the middle of exec, and reports that as EPERM after killing the rest.
                    # Repeat until the group is gone (ESRCH) or half a second has passed.
                    deadline = time.monotonic() + 0.5
                    while True:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except PermissionError:
                            if time.monotonic() >= deadline:
                                raise
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(0.01)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            except PermissionError as error:
                # macOS can deny signaling a group that exited between poll and killpg.
                # Ignore this only after wait confirms the child has finished.
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    stop_errors.append(error)
            except OSError as error:
                stop_errors.append(error)

    captured = {"stdout": _Capture(), "stderr": _Capture()}

    def drain(name: str, pipe: Any) -> None:
        # Each reader closes its own pipe, so nothing else ever waits on it.
        with pipe:
            while True:
                chunk = pipe.read(8192)
                if not chunk:
                    return
                captured[name].add(chunk)

    readers = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    remove_callback = token.on_cancel(stop)
    try:
        process.wait()
    except BaseException:
        stop()
        process.wait()
        raise
    finally:
        # A child that left the process group (a daemon, `setsid x &`) can hold the pipes open
        # long after the shell is gone. The readers get one second, then the call returns with
        # what was captured and they finish on their own.
        deadline = time.monotonic() + BASH_READER_GRACE_SECONDS
        for reader in readers:
            reader.join(max(0.0, deadline - time.monotonic()))
        remove_callback()
    with stopping:  # a stop that is still signalling the group finishes first
        pass
    if stop_errors:
        raise stop_errors[0]
    output = f"stdout:\n{captured['stdout'].text()}stderr:\n{captured['stderr'].text()}"
    if getattr(token, "timed_out", False):
        # The usual shape, with the exit code a shell gives a command that timed out.
        raise ToolError(f"exit_code: 124\n{output}" + ("" if output.endswith("\n") else "\n"))
    token.raise_if_cancelled()
    return f"exit_code: {process.returncode}\n{output}"


def bash_tool(*, cwd: str | None = None, env: Mapping[str, str] | None = None,
              timeout_seconds: float | None = None,
              approve: Callable[[str], Any] | None = None) -> Tool:
    """The guarded Bash tool. Its denied-command list is a guardrail, not a sandbox.

    approve(command) returns True to run it, or False or a reason to reject it.
    """

    def handler(arguments: JsonObject, context: ToolContext) -> str:
        command = arguments["command"]
        reason = denied_reason(command)
        if reason is None and approve is not None:
            decision = approve(command)
            if decision is not True:
                raise ToolError("Rejected by user" + (f": {decision}" if isinstance(decision, str) and decision else ""))
        return run_bash(command, context, cwd=cwd, env=env)

    return Tool(
        name="bash",
        description="Run a non-interactive bash command in the working directory.",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
        handler=handler,
        timeout_seconds=timeout_seconds,
    )


def command_approver(allow: Iterable[str] = (), ask: Callable[[str], str] | None = None) -> Callable[[str], Any]:
    """The CLI's approve callback: --allow patterns first, then ask the person.

    A pattern approves a command only when it matches the whole command, and
    never when the command contains a character that can chain, substitute or
    redirect. ask(command) returns what the person typed: y, n, a (allow
    everything from now on), or a reason for rejecting. Without ask, anything
    not approved by a pattern is rejected.
    """
    patterns = [re.compile(pattern) for pattern in allow]
    allow_all = False

    def approve(command: str) -> Any:
        nonlocal allow_all
        plain = not any(character in command for character in NEVER_AUTO_APPROVED)
        if allow_all or (plain and any(pattern.fullmatch(command) for pattern in patterns)):
            return True
        if ask is None:
            return "one-shot mode runs a command only with --yolo or a matching --allow"
        answer = ask(command).strip()
        if answer.lower() in ("a", "all"):
            allow_all = True
            return True
        if answer.lower() in ("y", "yes"):
            return True
        return False if answer.lower() in ("", "n", "no") else answer

    return approve


def _take(items: queue.SimpleQueue, token: CancellationToken) -> tuple[str, Any]:
    """The provider thread's next item. After cancellation it has one second to deliver one."""
    deadline = None
    while True:
        try:
            return items.get(timeout=0.05)
        except queue.Empty:
            if token.cancelled:
                deadline = deadline or time.monotonic() + GRACE_SECONDS
                if time.monotonic() >= deadline:
                    raise CancelledError("run aborted")


def _checked_turn(turn: Any) -> ProviderTurn:
    """A provider's turn, checked and with valid text, before it reaches the journal or a tool."""
    def count(value: Any) -> int:
        return value if type(value) is int and value >= 0 else 0

    if (not isinstance(turn, ProviderTurn) or not isinstance(turn.text, str) or not isinstance(turn.assistant, dict)
            or turn.assistant.get("role") != "assistant" or not isinstance(turn.tool_calls, list)
            or any(not isinstance(call, ToolCall) or not isinstance(call.call_id, str) or not call.call_id
                   or not isinstance(call.name, str) or not isinstance(call.arguments, dict)
                   for call in turn.tool_calls)
            or len({call.call_id for call in turn.tool_calls}) != len(turn.tool_calls)):
        raise ProviderError("the provider returned an invalid turn")
    return ProviderTurn(clean(turn.text), clean(turn.assistant), clean(turn.output),
                        [ToolCall(clean(call.call_id), clean(call.name), clean(call.arguments))
                         for call in turn.tool_calls], count(turn.input_tokens), count(turn.output_tokens))


def _tool_output(value: Any) -> Any:
    """What a handler returned, as a tool result: text, or a list of content parts."""
    if isinstance(value, list) and content_problem(value) is None:
        output: Any = clean(value)
    else:
        output = clean(value if isinstance(value, str) else json.dumps(value))
    _check_tool_output(output)
    return output


def _check_tool_output(output: Any) -> None:
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    if len(text.encode("utf-8")) > MAX_TOOL_OUTPUT_BYTES:
        raise ToolOutputLimitError(
            f"tool output exceeds {MAX_TOOL_OUTPUT_BYTES} UTF-8 bytes"
        )


def self_test() -> None:
    assert denied_reason("printf 'hello\\n'") is None
    for command in (
        "rm -rf /",
        'r""m -rf /',
        "sudo echo hi",
        "git reset --hard HEAD",
        "find . -delete",
        "mkfs.ext4 /dev/sda",
    ):
        assert denied_reason(command), command
    print("self-test passed")


def _number(text: str | None, kind: Callable[[str], Any]) -> Any:
    return kind(text) if text else None


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None,
         provider: Provider | None = None) -> int:
    """The CLI. Only this function reads the environment. Returns the exit code."""
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="tinyagent", description="A small coding agent with a Bash tool.")
    parser.add_argument("command", nargs="?", choices=["serve"], help="serve: HTTP server")
    parser.add_argument("-p", "--prompt", help="run one task and exit: 0 if it completed, 1 otherwise")
    parser.add_argument("--mode", choices=["rpc"], help="rpc: JSONL on stdin and stdout")
    parser.add_argument("--resume", metavar="SESSION_ID")
    parser.add_argument("--list-sessions", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--max-steps", type=int, default=_number(env.get("TINYAGENT_MAX_STEPS"), int))
    parser.add_argument("--max-seconds", type=float, default=_number(env.get("TINYAGENT_MAX_SECONDS"), float))
    parser.add_argument("--max-tokens", type=int, default=_number(env.get("TINYAGENT_MAX_TOKENS"), int))
    parser.add_argument("--cwd", help="directory the Bash tool runs in")
    parser.add_argument("--yolo", action="store_true", default=env.get("TINYAGENT_YOLO") == "1",
                        help="run commands without asking")
    parser.add_argument("--allow", action="append", default=[], metavar="REGEX",
                        help="run matching commands without asking (repeatable)")
    options = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if options.self_test:
        self_test()
        return 0
    directory = env.get("TINYAGENT_SESSION_DIR", ".tinyagent/sessions")
    if options.list_sessions:
        print("\n".join(Session.list(directory)))
        return 0
    context_size = env.get("TINYAGENT_CONTEXT_TOKENS")
    budget = (ContextBudget(int(context_size), int(env.get("TINYAGENT_RESPONSE_RESERVE", "4096")),
                            lambda request: len(json.dumps(request, ensure_ascii=False).encode()) + 1024)
              if context_size else None)
    provider_name = env.get("TINYAGENT_PROVIDER", "openai").lower()
    if provider_name not in ("openai", "anthropic", "chat"):
        raise SystemExit(f"unsupported TINYAGENT_PROVIDER: {provider_name}")
    key_name = "ANTHROPIC_API_KEY" if provider_name == "anthropic" else "OPENAI_API_KEY"
    model = env.get("TINYAGENT_MODEL") or {"anthropic": "claude-sonnet-4-6", "openai": MODEL}.get(provider_name)
    if not model:
        # A Chat Completions server can be anything, so there is no model to assume.
        raise SystemExit("TINYAGENT_MODEL is not set (required with TINYAGENT_PROVIDER=chat)")
    if provider is None:
        if not env.get(key_name) and provider_name != "chat":
            raise SystemExit(f"{key_name} is not set")
        base = {"base_url": env["TINYAGENT_BASE_URL"]} if env.get("TINYAGENT_BASE_URL") else {}
        kind = {"openai": OpenAIProvider, "anthropic": AnthropicProvider, "chat": ChatCompletionsProvider}
        # Many local servers need no key; an empty one sends no Authorization header.
        provider = kind[provider_name](env.get(key_name, ""), **base)
    interactive = options.command is None and options.mode is None and options.prompt is None

    def ask(command: str) -> str:
        # Control characters are shown escaped, so what is approved is exactly what will run.
        shown = "".join(character if character.isprintable() else repr(character)[1:-1] for character in command)
        return input(f"\n$ {shown}\nRun it? [y]es, [n]o, [a]ll, or a reason to reject: ")

    # RPC and HTTP clients cannot be asked, so those modes rely on the denied-command list alone.
    approve = (None if options.yolo or not (interactive or options.prompt is not None)
               else command_approver(options.allow, ask if interactive else None))
    agent = Agent(
        model=model, instructions=INSTRUCTIONS, provider=provider,
        tools=[bash_tool(cwd=options.cwd, timeout_seconds=120, approve=approve)],
        max_steps=options.max_steps, max_seconds=options.max_seconds, max_tokens=options.max_tokens,
    )
    if options.command == "serve":
        stopped = threading.Event()
        previous_term = signal.signal(
            signal.SIGTERM, lambda signum, frame: stopped.set()
        )
        try:
            with serve(
                agent,
                host=env.get("TINYAGENT_HOST", "127.0.0.1"),
                port=int(env.get("TINYAGENT_PORT", "8080")),
                token=env.get("TINYAGENT_TOKEN"),
                allow_remote=env.get("TINYAGENT_ALLOW_REMOTE") == "1",
                allowed_hosts=[name for name in env.get("TINYAGENT_ALLOWED_HOSTS", "").split(",") if name],
                allowed_origins=[name for name in env.get("TINYAGENT_ALLOWED_ORIGINS", "").split(",") if name],
                directory=directory,
                budget=budget,
            ) as server:
                print(f"TinyAgent listening at {server.url}", file=sys.stderr)
                try:
                    stopped.wait()
                except KeyboardInterrupt:
                    pass
        finally:
            signal.signal(signal.SIGTERM, previous_term)
        return 0
    if options.mode == "rpc":
        try:
            run_rpc(agent, directory=directory, session_id=options.resume, budget=budget)
        except BrokenPipeError:
            pass
        return 0

    def show(event: AgentEvent) -> None:
        if event.type == "text_delta":
            print(event.data["delta"], end="", flush=True)
        elif event.type == "tool_execution_start":
            print(f"\n$ {event.data['arguments'].get('command')!r}", file=sys.stderr)
        elif event.type == "tool_execution_end":
            print(event.data["output"], file=sys.stderr)

    session = (Session.open(agent, directory, options.resume, budget=budget)
               if options.resume else Session(agent, directory=directory, budget=budget))
    if options.prompt is not None:
        try:
            result = session.resume(on_event=show) if session.resumable and not session.paused else None
            if result is None or result.status == "completed":
                result = session.run(options.prompt, on_event=show)
            print()
            return 0 if result.status == "completed" else 1
        except Exception as error:
            print(f"\ntinyagent: {error}", file=sys.stderr)
            return 1
        finally:
            session.close()
    print(f"TinyAgent session {session.id}. Type a task, or Ctrl-C or Ctrl-D to exit.")
    try:
        if session.resumable and not session.paused:
            print("Continuing the interrupted run.")
            session.resume(on_event=show)
            print()
        while True:
            prompt = input("\n> ").strip()
            if prompt == "/resume" or prompt.startswith("/resume "):
                # A paused run takes the rest of the line as its tool result.
                answer = prompt[len("/resume "):] or None
                session.resume(result=answer, on_event=show)
                print()
                continue
            if prompt == "/abandon" or prompt.startswith("/abandon "):
                session.abandon(prompt[len("/abandon "):] or None)
                print("Abandoned the open run.")
                continue
            if prompt == "/history":
                print(json.dumps(session.records, ensure_ascii=False, indent=2))
                continue
            if prompt.startswith("/reconcile "):
                session.reconcile(prompt[len("/reconcile "):])
                continue
            if prompt == "/compact":
                session.compact()
                print("Compaction complete.")
                continue
            if prompt:
                session.run(prompt, on_event=show)
                print()
    except (EOFError, KeyboardInterrupt):
        print()
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
