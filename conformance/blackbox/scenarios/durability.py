"""Durable sessions: crash and resume, message IDs, abandon, replay, journal damage."""

from __future__ import annotations

import invariants
from fake_provider import Fault
from harness import BOTH, FINAL, Case, scenario
from wire import bash, say

INTERRUPTED = "This tool call was interrupted and may have partly run. Check its effects before continuing."


def _crash_mid_tool(case: Case, message_id: str | None = None):
    """Kill the target while its first command sleeps; reopen the session. The orphan still writes its effect."""
    link = case.link()
    prompt = case.script(bash(case.command("before")), bash(case.blocker("x", 0.4)), bash(case.command("after")),
                         say(FINAL))
    run = link.prompt(prompt, message_id=message_id, detach=True)
    case.wait_started("x")
    link.kill()
    run.drain()
    case.wait_children()
    return case.reopen_stored(), prompt


@scenario("D4, D38, B3, F4, C resume_run", variants=BOTH)
def crash_mid_tool_then_resume(case: Case) -> None:
    link, _ = _crash_mid_tool(case)
    state = link.state()
    case.equal((state.get("resumable"), state.get("recoveryRequired")), (True, False), "resumable, recoveryRequired")
    case.equal(link.history()["history"], [], "an open run is not committed history")
    before = len(case.provider.requests)
    run = case.completed(link.resume())
    case.equal(run.events[0].get("resumed"), True, "agent_start.resumed")
    start, end = run.of("tool_execution_start")[0], run.of("tool_execution_end")[0]
    case.equal(start.get("interrupted"), True, "start of the interrupted call carries interrupted:true (F4)")
    case.equal((end.get("output"), end.get("isError")), (INTERRUPTED, True), "result given for the interrupted call")
    told = [item["text"] for item in case.provider.requests[before].items if item["kind"] == "result"]
    case.equal(told[-1], INTERRUPTED, "what the model was told about the interrupted call")
    case.equal(dict(case.effects()), {"before": 1, "x": 1, "after": 1}, "effects: nothing ran twice")
    case.equal(run.end["usage"]["requests"], len(case.provider.requests) - before, "usage covers the resumed part (F10)")
    case.hold(invariants.agreement(link, run) + invariants.replay(link) + invariants.usable(case, link))


@scenario("D6: crash after a tool result", variants=BOTH)
def crash_after_result_then_resume(case: Case) -> None:
    link = case.link()
    case.stall(at=2)
    run = link.prompt(case.script(bash(case.command("x")), say(FINAL)), detach=True)
    case.wait_stalled()
    link.kill()
    run.drain()
    case.provider.release.set()
    link = case.reopen_stored()
    resumed = case.completed(link.resume())
    case.equal(resumed.of("tool_execution_start"), [], "a completed tool must not start again on resume")
    case.equal(case.effects().get("x"), 1, "effect of the completed command")
    case.equal(len(case.provider.requests), 3, "model requests: two before the crash, one after")
    case.hold(invariants.agreement(link, resumed) + invariants.replay(link))


@scenario("B3, C: errors around an open run", variants=BOTH)
def open_run_refuses_prompts(case: Case) -> None:
    link = case.link()
    case.fails("nothing_to_resume", lambda: link.resume(), "resume with nothing open")
    case.fails("nothing_to_resume", lambda: link.abandon(), "abandon with nothing open")
    case.equal(link.state().get("resumable"), False, "resumable on a fresh session")
    link.close()
    crashed = Case(case.target, case.wire, case.variant)
    try:
        link, _ = _crash_mid_tool(crashed)
        crashed.fails("resume_required", lambda: link.prompt("something new"), "a new prompt on an open run")
        case.equal(link.state().get("resumable"), True, "still resumable after the refused prompt")
        case.hold(invariants.usable(crashed, link, "resume") + crashed.finish())
    finally:
        crashed.cleanup()


@scenario("D36, F5, F3: abandon", variants=BOTH)
def abandon_open_run(case: Case) -> None:
    link, _ = _crash_mid_tool(case)
    before = len(case.provider.requests)
    case.equal(link.abandon("not needed"), {"abandoned": True}, "abandon reply")
    state = link.state()
    case.equal((state.get("resumable"), state.get("recoveryRequired")), (False, False), "state after abandon")
    events, problems = invariants.all_events(link)
    case.hold(problems)
    end = events[-1]
    case.equal((end["type"], end.get("status"), (end.get("error") or {}).get("code")),
               ("agent_end", "failed", "abandoned"), "replayed end of the abandoned run")
    case.equal(len(case.provider.requests), before, "model requests made by abandon")
    case.completed(link.prompt(case.script(say("next"))))
    case.equal([item["kind"] for item in case.provider.requests[-1].items], ["user"], "the next request (F20)")
    case.equal(dict(case.effects()), {"before": 1, "x": 1}, "effects")
    case.hold(invariants.replay(link))


@scenario("D10, B4, F9: repeated messageId after commit", variants=BOTH)
def repeated_message_id(case: Case) -> None:
    link = case.link()
    prompt = case.script(bash(case.command("x")), say(FINAL))
    first = case.completed(link.prompt(prompt, message_id="m-1"))
    case.check(not first.end.get("repeated") and not first.events[0].get("repeated"), "first run marked repeated")
    for attempt in ("same process", "after restart"):
        if attempt == "after restart":
            link = case.reopen(link)
        again = link.prompt(prompt, message_id="m-1").drain()
        case.equal(again.status, "completed", f"{attempt}: status")
        case.equal((again.events[0].get("repeated"), again.end.get("repeated")), (True, True),
                   f"{attempt}: repeated on agent_start and agent_end")
        case.equal((again.end.get("text"), again.end.get("usage")), (FINAL, first.end["usage"]),
                   f"{attempt}: stored text and usage")
        case.equal(again.of("tool_execution_start"), [], f"{attempt}: tool calls")
        if link.transport == "rpc":
            case.equal(again.data.get("repeated"), True, f"{attempt}: prompt response data.repeated")
    case.equal((len(case.provider.requests), case.effects().get("x")), (2, 1), "model requests, command effect")
    other = case.completed(link.prompt(case.script(say("different")), message_id="m-2"))
    case.check(not other.end.get("repeated"), "a new messageId must run")
    case.equal(len(link.history()["history"]), 6, "history holds each prompt once")


@scenario("D11, B4: repeated messageId on an open run resumes it; on a failed run runs again", variants=BOTH)
def message_id_on_open_and_failed_runs(case: Case) -> None:
    link, prompt = _crash_mid_tool(case, message_id="m-open")
    run = case.completed(link.prompt(prompt, message_id="m-open"))
    case.equal(run.events[0].get("resumed"), True, "agent_start.resumed for the repeated messageId")
    case.equal(dict(case.effects()), {"before": 1, "x": 1, "after": 1}, "effects")
    case.equal(len(link.history()["history"]), len(link.history()["history"]), "history")
    failing = case.script(say("second time lucky"))
    case.provider.fault_next(Fault("status", 401))
    case.equal(link.prompt(failing, message_id="m-fail").drain().status, "failed:provider_error", "first attempt")
    retried = case.completed(link.prompt(failing, message_id="m-fail"))
    case.check(not retried.end.get("repeated"), "a failed run's messageId must run again, not repeat")


@scenario("D12, D37, F2, F3, B5: replay", variants=BOTH)
def replay_events(case: Case) -> None:
    link = case.link()
    good = case.completed(link.prompt(case.script(bash(case.command("x"), text="Working. "), say(FINAL))))
    case.provider.fault_next(Fault("status", 401))
    bad = link.prompt(case.script(say("never"))).drain()
    case.stall()
    cut = link.prompt(case.script(say("never")))
    case.wait_stalled()
    link.abort()
    cut.drain()
    case.provider.release.set()
    events, problems = invariants.all_events(link, page=3)
    case.hold(problems + invariants.replay(link))
    by_run = {run_id: [e for e in events if e["runId"] == run_id] for run_id in dict.fromkeys(e["runId"] for e in events)}
    case.equal(list(by_run), [run.events[0]["runId"] for run in (good, bad, cut)], "runs in replay, in order")
    replayed = by_run[good.events[0]["runId"]]
    case.equal([(e["type"], e.get("delta")) for e in replayed],
               [("agent_start", None), ("text_delta", "Working. "), ("tool_execution_start", None),
                ("tool_execution_end", None), ("text_delta", FINAL), ("agent_end", None)],
               "replay of the completed run: one text_delta per stored assistant text")
    live = {(e["type"], e.get("toolCallId")): e for e in good.events if e["type"] != "text_delta"}
    for event in replayed:
        twin = live.get((event["type"], event.get("toolCallId")))
        if twin:
            case.equal(event["cursor"], twin.get("cursor"), f"replay cursor of {event['type']} against the live one (F2)")
            for key in ("output", "isError", "arguments", "executionId", "status", "text", "usage"):
                case.equal(event.get(key), twin.get(key), f"replayed {event['type']}.{key}")
    case.equal([e["sequence"] for e in replayed], list(range(1, len(replayed) + 1)), "replay sequence numbers")
    failed, aborted = by_run[bad.events[0]["runId"]][-1], by_run[cut.events[0]["runId"]][-1]
    case.equal((failed.get("status"), (failed.get("error") or {}).get("code")), ("failed", "provider_error"),
               "replayed end of the failed run (F3)")
    case.equal(aborted.get("status"), "aborted", "replayed end of the aborted run (F3)")
    middle = events[len(events) // 2]["cursor"]
    later = link.events(after=middle, limit=1000)["events"]
    case.equal([e["cursor"] for e in later], [e["cursor"] for e in events if e["cursor"] > middle], "events(after=n)")
    case.equal(link.events(after=events[-1]["cursor"]), {"events": [], "nextAfter": None}, "events after the last cursor")
    if link.transport == "rpc":
        state = link.call("get_state")
        case.check(state.get("usage") is not None, "get_state.usage after a run")


@scenario("D9: two writers on one session")
def two_writers(case: Case) -> None:
    first = case.rpc()
    case.completed(first.prompt(case.script(say("first writer"))))
    second = case.rpc(resume=first.session_id)
    try:
        run = second.prompt(case.script(bash(case.command("intruder")), say("second writer"))).drain()
        outcome = run.status
    except Exception as error:
        outcome = f"refused: {type(error).__name__}"
    case.note("second writer", outcome.split(":")[0])
    case.equal(case.effects().get("intruder", 0), 0, "a second writer must not execute a tool")
    case.check(outcome != "completed", "a second process ran a prompt on a session another process holds")
    case.completed(first.prompt(case.script(say("still mine"))), "the first writer afterwards")
    server = case.serve()
    case.fails(("session_busy", "session_fenced"), lambda: server.call("POST", "/sessions", {"sessionId": first.session_id}),
               "the HTTP server opening a session an RPC process holds")


@scenario("D24 storage: torn tail discarded, corruption refused")
def journal_damage(case: Case) -> None:
    link = case.rpc()
    case.completed(link.prompt(case.script(bash(case.command("x")), say(FINAL))))
    identifier = link.session_id
    link.close()
    journal = case.sessions / f"{identifier}.jsonl"
    whole = journal.read_bytes()
    journal.write_bytes(whole + whole.splitlines()[-1][: 40])           # a record cut off mid-write
    link = case.rpc(resume=identifier)
    case.equal(len(link.history()["history"]), 4, "history after a torn final record")
    case.completed(link.prompt(case.script(say("after the torn tail"))))
    case.hold(invariants.replay(link))
    link.close()
    lines = journal.read_bytes().splitlines(keepends=True)
    middle = bytearray(lines[2])
    spot = middle.index(b'"', 20) + 1
    middle[spot] = ord("Z") if middle[spot] != ord("Z") else ord("Y")
    journal.write_bytes(b"".join(lines[:2] + [bytes(middle)] + lines[3:]))
    damaged = case.rpc(resume=identifier)
    try:
        page = damaged.history()
        outcome = f"opened with {len(page.get('history', []))} history messages"
    except Exception as error:
        outcome = "refused"
    case.equal(outcome, "refused", "opening a journal with a corrupt earlier record")
    case.check(journal.read_bytes().count(b"\n") == len(lines), "a corrupt journal must not be rewritten")
