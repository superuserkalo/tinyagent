"""TinyAgent SDK and copyable streaming coding-agent CLI."""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
from typing import Any, Callable, Generator, Iterable

from tinyagent_types import (
    AgentEvent,
    CancellationToken,
    CancelledError,
    EventHandler,
    EventTransport,
    JsonObject,
    Provider,
    ProviderError,
    ProviderEvent,
    ProviderTurn,
    RunResult,
    Tool,
    ToolCall,
    ToolOutputLimitError,
    Transport,
    PROTOCOL_VERSION,
    MAX_JSONL_LINE_BYTES,
    MAX_HTTP_BODY_BYTES,
    MAX_QUEUED_OUTPUT_BYTES,
    MAX_TOOL_OUTPUT_BYTES,
)
from tinyagent_http import AgentServer, serve
from tinyagent_openai import OpenAIProvider, _CompleteTransportProvider
from tinyagent_anthropic import AnthropicProvider
from tinyagent_rpc import RpcSession, run_rpc
from tinyagent_session import Session, ContextBudget, RecoveryRequiredError
from tinyagent_storage import SessionBusyError, SessionCorruptionError

__version__ = "0.0.1"

MODEL = "gpt-5.6"
INSTRUCTIONS = """You are TinyAgent, a coding agent in the current directory.
Inspect before changing files. Use bash for shell and file work. Each call is a fresh process."""
DENIED_WORDS = {
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
}


class Agent:
    def __init__(
        self,
        *,
        model: str,
        instructions: str = "",
        tools: Iterable[Tool] = (),
        api_key: str | None = None,
        provider: Provider | None = None,
        transport: Transport | None = None,
    ) -> None:
        if provider is not None and transport is not None:
            raise ValueError("provide either provider or transport, not both")
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

    def stream(
        self,
        prompt: str,
        history: Iterable[JsonObject] = (),
        *,
        cancellation: CancellationToken | None = None,
        run_id: str | None = None,
        _record: Callable[[JsonObject], None] | None = None,
        _prepare: Callable[[list[JsonObject]], Generator[JsonObject, None, list[JsonObject]]] | None = None,
    ) -> Generator[AgentEvent, None, RunResult]:
        token = CancellationToken()
        transcript = copy.deepcopy(list(history))
        transcript.append({"role": "user", "content": prompt})
        run_id = run_id or f"r_{secrets.token_hex(12)}"
        sequence = 0
        terminal = False

        def event(event_type: str, **data: Any) -> AgentEvent:
            nonlocal sequence
            sequence += 1
            return AgentEvent(event_type, run_id, sequence, data)

        remove_parent = (
            cancellation.on_cancel(token.cancel) if cancellation else lambda: None
        )
        try:
            if _record:
                _record({"type": "item", "item": transcript[-1]})
            yield event("agent_start")
            while True:
                token.raise_if_cancelled()
                if _prepare:
                    preparation = _prepare(transcript)
                    try:
                        while True:
                            try:
                                progress = next(preparation)
                            except StopIteration as completed:
                                transcript = completed.value
                                break
                            yield event(progress["type"], **{key: value for key, value in progress.items() if key != "type"})
                    finally:
                        preparation.close()
                    token.raise_if_cancelled()
                provider_stream = self.provider.stream(
                    self.model,
                    self.instructions,
                    transcript,
                    self.tools.values(),
                    token,
                )
                try:
                    while True:
                        try:
                            provider_event = next(provider_stream)
                        except StopIteration as completed:
                            turn = completed.value
                            break
                        token.raise_if_cancelled()
                        if provider_event.type == "text_delta":
                            yield event("text_delta", delta=provider_event.delta)
                except (GeneratorExit, KeyboardInterrupt):
                    token.cancel()
                    raise
                finally:
                    provider_stream.close()
                token.raise_if_cancelled()
                transcript.append(turn.assistant)
                if _record:
                    _record({"type": "item", "item": turn.assistant})
                if not turn.tool_calls:
                    result = RunResult(turn.text, transcript, turn.output)
                    terminal = True
                    yield event("agent_end", status="completed", text=turn.text)
                    token.raise_if_cancelled()
                    return result
                for call in turn.tool_calls:
                    tool = self.tools.get(call.name)
                    if tool is None:
                        raise ProviderError(f"unknown tool: {call.name}")
                    if _record:
                        _record({"type": "tool_intent", "callId": call.call_id,
                                 "toolName": call.name, "arguments": call.arguments})
                    yield event(
                        "tool_execution_start",
                        toolCallId=call.call_id,
                        toolName=call.name,
                        arguments=call.arguments,
                    )
                    is_error = False
                    try:
                        token.raise_if_cancelled()
                        value = tool.execute(call.arguments, token)
                        token.raise_if_cancelled()
                        output = value if isinstance(value, str) else json.dumps(value)
                        _check_tool_output(output)
                    except CancelledError:
                        yield event(
                            "tool_execution_end",
                            toolCallId=call.call_id,
                            toolName=call.name,
                            output="run aborted",
                            isError=True,
                        )
                        raise
                    except Exception as error:
                        is_error = True
                        output = f"{type(error).__name__}: {error}"
                        if len(output.encode("utf-8")) > MAX_TOOL_OUTPUT_BYTES:
                            output = f"ToolOutputLimitError: tool error exceeds {MAX_TOOL_OUTPUT_BYTES} UTF-8 bytes"
                    observation = {
                        "role": "tool_result", "callId": call.call_id,
                        "toolName": call.name, "output": output, "isError": is_error,
                    }
                    if _record:
                        _record({"type": "item", "item": observation})
                    yield event(
                        "tool_execution_end",
                        toolCallId=call.call_id,
                        toolName=call.name,
                        output=output,
                        isError=is_error,
                    )
                    transcript.append(observation)
        except Exception as error:
            if terminal:
                raise
            if token.cancelled or isinstance(error, CancelledError):
                yield event("agent_end", status="aborted")
                if isinstance(error, CancelledError):
                    raise
                raise CancelledError("run aborted") from error
            yield event(
                "agent_end",
                status="failed",
                error={
                    "code": "provider_error",
                    "message": str(error),
                    "retryable": False,
                },
            )
            raise
        finally:
            remove_parent()
            token.cancel()

    def run(
        self,
        prompt: str,
        history: Iterable[JsonObject] = (),
        *,
        cancellation: CancellationToken | None = None,
        on_event: EventHandler | None = None,
        run_id: str | None = None,
    ) -> RunResult:
        events = self.stream(prompt, history, cancellation=cancellation, run_id=run_id)
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


def run_bash(command: str, cancellation: CancellationToken | None = None) -> str:
    reason = denied_reason(command)
    if reason:
        return f"exit_code: 126\nstdout:\n\nstderr:\nblocked: {reason}\n"
    token = cancellation or CancellationToken()
    token.raise_if_cancelled()
    process = subprocess.Popen(
        ["bash", "-c", command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    stop_errors: list[OSError] = []

    def stop() -> None:
        # Descendants can still own the pipes after their shell has exited.
        if os.name != "posix" and process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
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

    captured = {"stdout": bytearray(), "stderr": bytearray()}
    captured_bytes = 0
    output_too_large = False
    capture_lock = threading.Lock()

    def drain(name: str, pipe: Any) -> None:
        nonlocal captured_bytes, output_too_large
        while True:
            chunk = pipe.read(8192)
            if not chunk:
                return
            exceeded = False
            with capture_lock:
                if output_too_large:
                    continue
                if captured_bytes + len(chunk) > MAX_TOOL_OUTPUT_BYTES:
                    output_too_large = True
                    exceeded = True
                else:
                    captured_bytes += len(chunk)
                    captured[name].extend(chunk)
            if exceeded:
                stop()

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
        for reader in readers:
            reader.join()
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()
        remove_callback()
    if stop_errors:
        raise stop_errors[0]
    token.raise_if_cancelled()
    if output_too_large:
        raise ToolOutputLimitError(
            f"tool output exceeds {MAX_TOOL_OUTPUT_BYTES} UTF-8 bytes"
        )
    stdout = captured["stdout"].decode("utf-8", errors="replace")
    stderr = captured["stderr"].decode("utf-8", errors="replace")
    observation = f"exit_code: {process.returncode}\nstdout:\n{stdout}stderr:\n{stderr}"
    _check_tool_output(observation)
    return observation


def bash_tool(arguments: JsonObject, cancellation: CancellationToken) -> str:
    command = arguments.get("command")
    if not isinstance(command, str):
        raise RuntimeError("bash requires a string command")
    print(f"\n$ {command}", file=sys.stderr)
    observation = run_bash(command, cancellation)
    print(observation, file=sys.stderr)
    return observation


def _check_tool_output(output: str) -> None:
    if len(output.encode("utf-8")) > MAX_TOOL_OUTPUT_BYTES:
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


def main() -> None:
    if "--self-test" in sys.argv:
        self_test()
        return
    directory = os.getenv("TINYAGENT_SESSION_DIR", ".tinyagent/sessions")
    if "--list-sessions" in sys.argv:
        print("\n".join(Session.list(directory)))
        return
    resume_index = sys.argv.index("--resume") if "--resume" in sys.argv else -1
    if resume_index >= 0 and resume_index + 1 >= len(sys.argv):
        raise SystemExit("--resume requires a session ID")
    session_id = sys.argv[resume_index + 1] if resume_index >= 0 else None
    context_size = os.getenv("TINYAGENT_CONTEXT_TOKENS")
    budget = (ContextBudget(int(context_size), int(os.getenv("TINYAGENT_RESPONSE_RESERVE", "4096")),
                            lambda request: len(json.dumps(request, ensure_ascii=False).encode()) + 1024)
              if context_size else None)
    provider_name = os.getenv("TINYAGENT_PROVIDER", "openai").lower()
    if provider_name == "anthropic":
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY is not set")
        provider: Provider = AnthropicProvider()
        model = os.getenv("TINYAGENT_MODEL", "claude-sonnet-4-6")
    elif provider_name == "openai":
        if not os.getenv("OPENAI_API_KEY"):
            raise SystemExit("OPENAI_API_KEY is not set")
        provider = OpenAIProvider()
        model = os.getenv("TINYAGENT_MODEL", MODEL)
    else:
        raise SystemExit(f"unsupported TINYAGENT_PROVIDER: {provider_name}")
    bash = Tool(
        name="bash",
        description="Run a non-interactive bash command in the current directory.",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
        handler=bash_tool,
    )
    agent = Agent(
        model=model, instructions=INSTRUCTIONS, tools=[bash], provider=provider
    )
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        stopped = threading.Event()
        previous_term = signal.signal(
            signal.SIGTERM, lambda signum, frame: stopped.set()
        )
        try:
            with serve(
                agent,
                host=os.getenv("TINYAGENT_HOST", "127.0.0.1"),
                port=int(os.getenv("TINYAGENT_PORT", "8080")),
                token=os.getenv("TINYAGENT_TOKEN"),
                allow_remote=os.getenv("TINYAGENT_ALLOW_REMOTE") == "1",
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
        return
    mode_index = sys.argv.index("--mode") if "--mode" in sys.argv else -1
    if mode_index >= 0:
        if mode_index + 1 >= len(sys.argv) or sys.argv[mode_index + 1] != "rpc":
            raise SystemExit("supported mode: rpc")
        try:
            run_rpc(agent, directory=directory, session_id=session_id, budget=budget)
        except BrokenPipeError:
            pass
        return
    session = (Session.open(agent, directory, session_id, budget=budget)
               if session_id else Session(agent, directory=directory, budget=budget))
    print(f"TinyAgent session {session.id}. Type a task, or Ctrl-C or Ctrl-D to exit.")
    try:
        while True:
            prompt = input("\n> ").strip()
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
                session.run(
                    prompt,
                    on_event=lambda event: (
                        print(event.data["delta"], end="", flush=True)
                        if event.type == "text_delta"
                        else None
                    ),
                )
                print()
    except (EOFError, KeyboardInterrupt):
        print()
    finally:
        session.close()


if __name__ == "__main__":
    main()
