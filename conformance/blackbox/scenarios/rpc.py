"""RPC mode: framing, command errors, process exit, and the session commands."""

from __future__ import annotations

import signal

import invariants
from driver import wait_until
from harness import FINAL, Case, scenario
from wire import bash, say


def _protocol_error(case: Case, link, code: str) -> None:
    event = link.event()
    case.check(event is not None and event.get("type") == "protocol_error" and event["error"].get("code") == code,
               f"expected protocol_error {code}, got {event}")


@scenario("D24 v1#10, PROTOCOL RPC commands and errors, C get_state")
def rpc_framing_and_errors(case: Case) -> None:
    link = case.rpc()
    state = link.call("get_state")
    case.equal({key: state.get(key, "missing") for key in ("protocolVersion", "status", "runId", "messageCount",
                                                           "resumable", "usage")},
               {"protocolVersion": 2, "status": "idle", "runId": None, "messageCount": 0, "resumable": False,
                "usage": None}, "idle get_state")
    case.equal(link.call("abort"), {"accepted": False, "runId": None}, "idle abort")
    case.fails("invalid_command", lambda: link.call("not_a_command"), "unknown command with an id")
    case.fails("invalid_command", lambda: link.call("prompt", message=""), "empty prompt")
    link.send({"type": "not_a_command"})
    _protocol_error(case, link, "invalid_command")
    link.process.write(b"{not json\n")
    _protocol_error(case, link, "invalid_json")
    link.process.write(b"[1, 2]\n")
    _protocol_error(case, link, "invalid_command")
    link.send({"id": 7, "type": "get_state"})
    _protocol_error(case, link, "invalid_command")
    link.process.write(b'{"id":"multi","type":"get_state"}\n{"id":"line","type":"get_state"}\n')
    wait_until(lambda: "line" in link._responses, what="two commands sent in one write")
    case.equal(link.call("get_state")["status"], "idle", "still serving after protocol errors")
    case.equal(link.close(), 0, "exit code after EOF")


@scenario("D24 limits: oversized line and invalid UTF-8 end RPC mode")
def rpc_fatal_input(case: Case) -> None:
    for payload, code in ((b'{"type":"prompt","message":"' + b"x" * 1_048_577 + b'"}\n', "line_too_large"),
                          (b'{"type":"get_state","id":"\xff\xfe"}\n', "invalid_utf8")):
        link = case.rpc()
        link.process.write(payload)
        _protocol_error(case, link, code)
        link.process.wait(5)


@scenario("D24 v1#10: EOF and Ctrl-C cancel the run and exit; invariant 8")
def rpc_eof_and_sigint(case: Case) -> None:
    for how in ("eof", "sigint"):
        link = case.rpc()
        run = link.prompt(case.script(bash(case.blocker(how)), say("never")))
        case.wait_started(how)
        if how == "eof":
            link.process.close_stdin()
        else:
            link.process.signal(signal.SIGINT)
        link.process.wait(5)
        run.drain(5)
        case.note(f"stream at {how}", run.status)
        case.check(run.status in ("aborted", "died"), f"{how}: the run ended {run.status}")
        case.wait_children(3)
        case.equal(case.effects().get(how, 0), 0, f"effect of the command running at {how}")
    stored = case.rpc(resume=case.session_ids()[0])
    case.note("after EOF mid-run the session is", "resumable" if stored.state().get("resumable") else "idle")
    case.hold(invariants.usable(case, stored))


@scenario("D24 PROTOCOL durable RPC commands: list, new, resume_session, get_history paging; D1 gapless records")
def rpc_sessions_and_history(case: Case) -> None:
    link = case.rpc()
    first = link.session_id
    case.check(first.startswith("s_"), f"session id: {first}")
    case.completed(link.prompt(case.script(bash(case.command("x")), say(FINAL))))
    case.equal(link.call("new_session"), {"abortedRun": False}, "new_session reply")
    second = link.session_id
    case.check(second != first and link.call("get_state")["messageCount"] == 0, "new_session must start empty")
    case.completed(link.prompt(case.script(say("second session"))))
    case.equal(sorted(link.call("list_sessions")["sessionIds"]), sorted([first, second]), "list_sessions")
    case.equal(link.call("resume_session", sessionId=first), {"sessionId": first}, "resume_session reply")
    history = link.history()["history"]
    case.equal([entry["role"] for entry in history], ["user", "assistant", "tool_result", "assistant"], "history roles")
    page = link.history(limit=1)
    case.equal((len(page["history"]), page["nextOffset"]), (1, 1), "first page of one")
    case.equal(link.history(offset=3, limit=5)["nextOffset"], None, "nextOffset on the last page")
    records = link.history(view="records")["records"]
    case.equal([record.get("sequence") for record in records], list(range(1, len(records) + 1)), "record sequences")
    kinds = [record.get("type") for record in records]
    for kind in ("run_start", "tool_intent", "tool_result", "run_commit"):
        case.check(kind in kinds, f"journal has no {kind} record: {kinds}")
    case.fails("session_error", lambda: link.history(limit=0), "limit 0")
    case.fails("session_error", lambda: link.call("resume_session", sessionId="s_doesnotexist"), "unknown session")
    case.equal(link.session_id, first, "a failed resume_session keeps the current session")


@scenario("D24 compaction; PROTOCOL compaction events and errors", variants=("rpc", "http"))
def compaction(case: Case) -> None:
    link = case.link()
    code = "session_error" if link.transport == "rpc" else "budget_required"
    case.fails(code, lambda: link.compact(), "compact without a budget")
    link.close()
    budgeted = Case(case.target, case.wire, case.variant).configure(
        TINYAGENT_CONTEXT_TOKENS="400000", TINYAGENT_RESPONSE_RESERVE="1000")
    try:
        link = budgeted.link()
        for number in range(5):
            budgeted.completed(link.prompt(budgeted.script(say(f"answer {number} " + "padding " * 200))))
        before = len(str(budgeted.provider.requests[-1].body))
        run = link.compact().drain()
        case.equal([event["type"] for event in run.events], ["compaction_start", "compaction_end"], "compaction events")
        case.equal((run.end.get("status"), run.end.get("changed")), ("completed", True), "compaction_end")
        summary = budgeted.provider.requests[-1]
        case.check(not summary.body.get("tools"), "the summary request must have tools disabled")
        again = link.compact().drain()
        case.equal((again.end.get("status"), again.end.get("changed")), ("completed", False), "nothing left to compact")
        budgeted.completed(link.prompt(budgeted.script(say("after compaction"))))
        after = budgeted.provider.requests[-1]
        case.check("Summary:" in str(after.body) and len(str(after.body)) < before,
                   "the request after compaction should carry the summary in place of older history")
        case.equal(len(link.history()["history"]), 12, "raw history is kept after compaction")
        case.hold(invariants.replay(link) + budgeted.finish())
    finally:
        budgeted.cleanup()
