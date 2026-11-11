"""The agent loop as seen on the wire: text, tools, errors, retry, limits, cancellation."""

from __future__ import annotations

import invariants
from fake_provider import Fault
from harness import BOTH, FINAL, MODEL, Case, scenario
from wire import WIRES, Call, Turn, bash, say

USAGE = {"inputTokens": 11, "outputTokens": 7, "requests": 1}
V1_EVENTS = {"agent_start", "text_delta", "tool_execution_start", "tool_execution_end", "agent_end"}


@scenario("D24 v1#1, A2, F15 provider selection, D25 key header, D39 trace off", variants=BOTH, wires=WIRES)
def text_only(case: Case) -> None:
    text = "Héllo wörld, the answer is 42 ✓"
    link = case.link()
    run = case.completed(link.prompt(case.script(say(text))))
    kinds = [event["type"] for event in run.events]
    case.equal(kinds, ["agent_start"] + ["text_delta"] * (len(kinds) - 2) + ["agent_end"], "event types")
    case.check(len(kinds) > 3, "text arrived as one delta; the provider sent several")
    case.equal(run.text, text, "concatenated deltas")
    case.equal(run.end.get("text"), text, "agent_end.text")
    case.equal(run.end.get("usage"), USAGE, "agent_end.usage")
    request = case.provider.requests[0]
    case.equal(request.wire, case.wire, "provider API used")
    case.equal(request.body.get("model"), MODEL, "model in the request")
    case.check(request.body.get("tools"), "the bash tool definition is missing from the request")
    if case.wire == "anthropic":
        case.equal(request.headers.get("x-api-key"), "test-key", "x-api-key header")
        case.check(request.headers.get("anthropic-version"), "anthropic-version header is missing")
    else:
        case.equal(request.headers.get("authorization"), "Bearer test-key", "Authorization header")
    case.hold(invariants.agreement(link, run, 0))


@scenario("D24 v1#2 #4, B2 executionId, F2 live cursors, F15 no stderr echo", variants=BOTH, wires=WIRES)
def tool_roundtrip(case: Case) -> None:
    link = case.link()
    command = 'echo "résultat $((6 * 7))"'
    run = case.completed(link.prompt(case.script(bash(command, text="Let me check. "), bash(case.command("x")),
                                                 say(FINAL))))
    case.equal([event["type"] for event in run.events if event["type"] != "text_delta"],
               ["agent_start", "tool_execution_start", "tool_execution_end", "tool_execution_start",
                "tool_execution_end", "agent_end"], "event order")
    case.check(not {event["type"] for event in run.events} - V1_EVENTS, "trace events appeared without trace")
    start, end = run.of("tool_execution_start")[0], run.of("tool_execution_end")[0]
    case.equal(start.get("arguments"), {"command": command}, "tool arguments")
    case.equal(start.get("toolName"), "bash", "tool name")
    case.equal(start.get("executionId"), f"{link.session_id}:{start.get('cursor')}", "executionId (B2)")
    case.check("résultat 42" in end.get("output", "") and end.get("isError") is False, f"tool output: {end}")
    for event in run.events:
        case.equal("cursor" in event, event["type"] != "text_delta", f"cursor present on live {event['type']} (F2)")
    results = [item for item in case.provider.requests[1].items if item["kind"] == "result"]
    case.check(len(results) == 1 and "résultat 42" in results[0]["text"], "the model did not receive the tool output")
    case.equal(run.end["usage"], {"inputTokens": 33, "outputTokens": 21, "requests": 3}, "usage summed over the run")
    case.equal(case.effects().get("x"), 1, "command effect")
    case.check("résultat" not in case.stderr_tail() and "echo x" not in case.stderr_tail(),
               "RPC and HTTP modes must not echo commands or output to stderr (F15)")
    case.hold(invariants.agreement(link, run, 0))


@scenario("D24 v1#3 #5, tool argument validation, denied command", variants=BOTH)
def tool_errors_are_recoverable(case: Case) -> None:
    link = case.link()
    run = case.completed(link.prompt(case.script(
        bash("git reset --hard HEAD"),                       # on the destructive-command list; harmless here
        Turn(calls=[Call(raw='{"wrong": 1}')]),              # fails schema validation
        bash("echo oops >&2; exit 3"), say(FINAL))))
    denied, invalid, failing = run.of("tool_execution_end")
    case.check("denied" in denied["output"] or "blocked" in denied["output"], f"a denied command says so: {denied}")
    case.check(denied["isError"] is True, f"a denied command must be a tool error: {denied}")
    case.check(invalid["isError"] is True, f"invalid arguments must be a tool error: {invalid}")
    case.check("oops" in failing["output"] and "3" in failing["output"], f"stderr and exit code: {failing}")
    case.note("non-zero exit isError", failing["isError"])
    case.equal(len(case.provider.requests), 4, "model requests")


@scenario("D15, A3", variants=BOTH, wires=WIRES)
def unknown_tool_then_repeated_errors(case: Case) -> None:
    link = case.link()
    nope = lambda: Turn(calls=[Call("true", name="nope")])
    run = case.completed(link.prompt(case.script(nope(), nope(), say(FINAL))))
    end = run.of("tool_execution_end")[0]
    case.check(end["isError"] is True and "Unknown tool: nope" in end["output"], f"unknown tool observation: {end}")
    failed = link.prompt(case.script(nope(), nope(), nope(), nope(), say("unreachable"))).drain()
    case.equal(failed.status, "failed:repeated_tool_errors", "status after three all-error turns")
    case.equal(failed.end["error"].get("retryable"), False, "retryable")
    case.equal(failed.end["usage"]["requests"], 3, "model requests before giving up")
    case.hold(invariants.agreement(link, failed) + invariants.usable(case, link))


@scenario("D26 missing usage", variants=("rpc",), wires=WIRES)
def missing_usage(case: Case) -> None:
    link = case.link()
    run = case.completed(link.prompt(case.script(bash(case.command("x"), usage=None), say(FINAL, usage=None))))
    case.equal(run.end.get("usage"), {"inputTokens": 0, "outputTokens": 0, "requests": 2}, "usage without provider usage")


@scenario("D24 v1#8 #9, B4 active -> busy", variants=BOTH)
def busy_and_rollback(case: Case) -> None:
    link = case.link()
    case.stall()
    run = link.prompt(case.script(say("never")), message_id="m1")
    case.wait_stalled()
    busy = "busy" if link.transport == "rpc" else "session_busy"
    case.fails(busy, lambda: link.prompt("second prompt"), "prompt while a run is active")
    case.fails(busy, lambda: link.prompt("same id", message_id="m1"), "active messageId")
    if link.transport == "rpc":
        state = link.call("get_state")
        case.equal((state["status"], state["runId"]), ("running", run.data["runId"]), "get_state while running")
        for command in ("get_history", "get_events", "list_sessions"):
            case.fails("busy", lambda: link.call(command), f"{command} while a run is active")
    case.equal(link.abort().get("accepted"), True, "abort accepted")
    case.equal(run.drain().status, "aborted", "status after abort")
    case.hold(invariants.agreement(link, run, 0))
    after = case.completed(link.prompt(case.script(say("fresh"))))
    case.equal([item["kind"] for item in case.provider.requests[-1].items], ["user"], "items in the next request (F20)")
    history = link.history()["history"]
    case.equal([entry.get("role") for entry in history], ["user", "assistant"], "committed history")


@scenario("D16, A4, F10, F11", variants=("rpc",), wires=("openai", "chat"))
def provider_retry(case: Case) -> None:
    link = case.link()
    provider = case.provider

    def gap(fault: Fault) -> float:
        first = len(provider.requests)
        provider.fault(first + 1, fault)
        run = case.completed(link.prompt(case.script(say("ok"))))
        case.equal(len(provider.requests), first + 2, "requests for one retried call")
        case.equal(run.end["usage"]["requests"], 1, "usage.requests for a retried request (F10)")
        return provider.requests[first + 1].at - provider.requests[first].at

    plain = gap(Fault("status", 429))
    case.check(0.8 <= plain <= 3, f"backoff before the first retry should be 1 s, was {plain:.2f} s")
    zero = gap(Fault("status", 429, retry_after="0"))
    case.check(zero < 0.7, f"Retry-After: 0 should retry at once, waited {zero:.2f} s")
    two = gap(Fault("status", 503, retry_after="2"))
    case.check(1.8 <= two <= 4, f"Retry-After: 2 should wait 2 s, waited {two:.2f} s")
    date = gap(Fault("status", 503, retry_after="Wed, 21 Oct 2037 07:28:00 GMT"))
    case.check(0.8 <= date <= 3, f"an HTTP-date Retry-After is ignored (F11); waited {date:.2f} s")
    first = len(provider.requests)
    provider.fault_always(Fault("status", 503, retry_after="0"))
    run = link.prompt(case.script(say("never"))).drain()
    provider.fault_always(None)
    case.equal(run.status, "failed:provider_error", "status after every attempt failed")
    case.equal(len(provider.requests) - first, 4, "attempts in total (A4)")
    case.hold(invariants.agreement(link, run) + invariants.usable(case, link))


@scenario("D16 cancellation during backoff", variants=BOTH)
def cancel_during_backoff(case: Case) -> None:
    link = case.link()
    case.provider.fault(1, Fault("status", 429, retry_after="30"))
    run = link.prompt(case.script(say("never")))
    case.provider.wait_requests(1)
    run.take(1)
    case.equal(link.abort().get("accepted"), True, "abort accepted during backoff")
    case.equal(run.drain(5).status, "aborted", "status")
    case.equal(len(case.provider.requests), 1, "requests after cancelling during backoff")
    case.hold(invariants.usable(case, link))


@scenario("D24 v1#7: cancel during provider streaming and during bash", variants=BOTH, wires=WIRES)
def cancel_mid_stream_and_mid_bash(case: Case) -> None:
    link = case.link()
    case.stall(mid=True)
    run = link.prompt(case.script(say("a long answer that is cut off in the middle")))
    case.wait_stalled()
    link.abort()
    case.equal(run.drain(5).status, "aborted", "status after cancel during streaming")
    run = link.prompt(case.script(bash(case.blocker("slow")), say("never")))
    case.wait_started("slow")
    link.abort()
    case.equal(run.drain(5).status, "aborted", "status after cancel during bash")
    end = run.of("tool_execution_end")
    case.check(len(end) == 1 and end[0]["isError"], "the cancelled tool must still report an end")
    case.wait_children(3)
    case.equal(case.effects().get("slow", 0), 0, "effect of the cancelled command")
    case.hold(invariants.agreement(link, run, 0) + invariants.usable(case, link))


@scenario("D14, A1, F10", variants=BOTH)
def limits(case: Case) -> None:
    def limited(option: str, value: str, slow: float = 0.0) -> tuple:
        inner = Case(case.target, case.wire, case.variant)
        try:
            if option.startswith("--"):
                inner.configure(option, value)
            else:
                inner.configure(**{option: value})
            link = inner.link()
            prompt, _ = inner.standard(slow)
            run = link.prompt(prompt).drain()
            case.hold(invariants.agreement(link, run, 0) + invariants.usable(inner, link) + inner.finish())
            return run, len(inner.provider.requests) - 1, dict(inner.effects())
        finally:
            inner.cleanup()

    for option, value, slow, limit, requests in (("--max-steps", "2", 0, "steps", 2),
                                                 ("TINYAGENT_MAX_STEPS", "1", 0, "steps", 1),
                                                 ("--max-tokens", "30", 0, "tokens", 2),
                                                 ("TINYAGENT_MAX_TOKENS", "18", 0, "tokens", 1),
                                                 ("--max-seconds", "0.3", 0.6, "seconds", 2),
                                                 ("TINYAGENT_MAX_SECONDS", "0.3", 0.6, "seconds", 2)):
        run, seen, effects = limited(option, value, slow)
        what = f"{option}={value}"
        case.equal(run.status, "failed:limit_exceeded", f"{what}: status")
        error = run.end["error"]
        case.equal((error.get("limit"), error.get("retryable")), (limit, False), f"{what}: error.limit, retryable")
        case.equal(seen, requests, f"{what}: model requests made before the limit")
        case.equal(run.end.get("usage", {}).get("requests"), requests, f"{what}: usage.requests")
        case.equal(effects.get("a"), 1, f"{what}: the first command still ran")


@scenario("D18, A6", variants=("rpc",))
def bash_output_truncation(case: Case) -> None:
    link = case.link()
    run = case.completed(link.prompt(case.script(
        bash("head -c 100000 /dev/zero | tr '\\0' x; echo; echo tail-marker"), say(FINAL))))
    end = run.of("tool_execution_end")[0]
    case.check(end["isError"] is False, f"large output must not be an error: {end['output'][:200]}")
    case.check("[... 70" in end["output"] and "bytes elided ...]" in end["output"],
               f"elided line missing; output is {len(end['output'])} bytes: {end['output'][:120]!r}")
    case.check("tail-marker" in end["output"] and len(end["output"]) < 31_000, "head and tail must both be kept")


@scenario("D19 cwd (env is SDK-only)", variants=BOTH)
def bash_cwd(case: Case) -> None:
    directory = case.root / "elsewhere"
    directory.mkdir()
    case.configure("--cwd", str(directory))
    link = case.link()
    run = case.completed(link.prompt(case.script(bash("pwd"), say(FINAL))))
    case.check(str(directory) in run.of("tool_execution_end")[0]["output"], "bash did not run in --cwd")


@scenario("D32, E7", variants=BOTH, wires=WIRES)
def image_input(case: Case) -> None:
    link = case.link()
    url, data = "https://example.invalid/cat.png", "iVBORw0KGgo="
    message = [{"type": "text", "text": case.script(say("a cat"))}, {"type": "image", "url": url},
               {"type": "image", "mediaType": "image/png", "data": data}]
    case.completed(link.prompt(message))
    content = case.provider.requests[0].items[0]["raw"]["content"]
    case.check(isinstance(content, list) and len(content) == 3, f"user content parts: {str(content)[:200]}")
    expected = {
        "openai": [{"type": "input_image", "image_url": url},
                   {"type": "input_image", "image_url": f"data:image/png;base64,{data}"}],
        "anthropic": [{"type": "image", "source": {"type": "url", "url": url}},
                      {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}],
        "chat": [{"type": "image_url", "image_url": {"url": url}},
                 {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}],
    }[case.wire]
    for want, got in zip(expected, content[1:]):
        case.check(all(got.get(key) == value for key, value in want.items()), f"image part: want {want}, got {got}")
    case.hold(invariants.usable(case, link))
