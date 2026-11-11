"""Sweep c: cancel after every event of the conversation (RPC `abort`, HTTP `cancel`)."""

from __future__ import annotations

from typing import Any

import invariants
from harness import Case, Spec
from sweeps.common import SLOW, THINK, baseline, broken, check_effects, settle


def cancel(case: Case, after: int, final_turn: int) -> None:
    link = case.link()
    prompt, tags = case.standard(SLOW)
    run = link.prompt(prompt).take(after)
    case.equal(len(run.events), after, "events received before cancelling")
    last = run.events[-1]
    reply = link.abort()
    run.drain()
    case.check(run.end is not None, f"no terminal event after cancel: {run.status}")
    if after < final_turn:
        case.equal(reply.get("accepted"), True, "cancel accepted while the run is active")
        case.equal(reply.get("runId"), last.get("runId"), "cancel names the active run")
        case.equal(run.status, "aborted", "status of a run cancelled before its last model turn")
    else:
        case.check(run.status in ("aborted", "completed"), f"cancelled in the last turn, the run ended {run.status}")
        case.note("~outcome", run.status)
    case.equal(link.abort().get("accepted"), False, "cancel with no active run")
    if last.get("type") == "tool_execution_start" and tags.get(last.get("toolCallId")) == "b" and run.status == "aborted":
        case.wait_children()
        case.equal(case.effects().get("b", 0), 0, "effect of the slow command cancelled while it slept "
                   "(cancellation must terminate the command's process group)")
    check_effects(case, [run], tags)
    case.hold(invariants.agreement(link, run, messages_before=0, journal_wins=True))
    committed = bool(link.history().get("history"))
    settle(case, link, tags, committed=committed)


def specs(target: Any, options: Any) -> list[Spec]:
    out = []
    for transport in ("rpc", "http"):
        measured = baseline(target, transport)
        if measured.error:
            out += broken("cancel", transport, measured)
            continue
        out += [Spec(f"cancel[{transport},after={index}]", "cancel",
                     lambda case, i=index, f=measured.final_turn: cancel(case, i, f),
                     variant=transport, think=THINK)
                for index in range(1, measured.events)]
    return out
