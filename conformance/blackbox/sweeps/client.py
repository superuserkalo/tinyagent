"""Sweep d: an HTTP client that disconnects after every event, with and without `detach`.

Without detach the disconnect cancels the run. With detach the run goes on, and
the client reconnects with `events?after=N&follow=1` and must receive the rest
exactly once.
"""

from __future__ import annotations

from typing import Any

import invariants
from driver import wait_until
from harness import FINAL, Case, Spec
from sweeps.common import SLOW, THINK, baseline, broken, check_effects, settle


def _recorded_end(link: Any, run_id: str) -> dict | None:
    events = link.events(after=0, limit=1000).get("events") or []
    return next((e for e in events if e.get("runId") == run_id and e.get("type") == "agent_end"), None)


def disconnect(case: Case, after: int, detach: bool, final_turn: int) -> None:
    link = case.http()
    prompt, tags = case.standard(SLOW)
    run = link.prompt(prompt, detach=detach).take(after)
    case.equal(len(run.events), after, "events received before disconnecting")
    run_id = run.events[0]["runId"]
    seen = [event["cursor"] for event in run.events if "cursor" in event]
    run.disconnect()
    if detach:
        follow = link.follow(after=seen[-1]).drain()
        cursors = [event["cursor"] for event in follow.events if "cursor" in event]
        case.check(not set(cursors) & set(seen), f"follow re-delivered events the client already had: {cursors}")
        case.check(len(set(cursors)) == len(cursors) and cursors == sorted(cursors),
                   f"follow cursors are not strictly increasing: {cursors}")
        case.check(follow.end is not None and follow.end.get("runId") == run_id,
                   f"follow after cursor {seen[-1]} did not end with the run's agent_end; it delivered "
                   f"{[(e['type'], e.get('cursor')) for e in follow.events]} and then closed")
        case.equal(follow.status, "completed", "status of a detached run after its client left")
        recorded = _recorded_end(link, run_id)
        case.equal((recorded or {}).get("status"), "completed", "recorded status of the detached run")
        def journaled(events: list[dict]) -> list[int]:      # stored text has a cursor only in replay (F2)
            return [e["cursor"] for e in events if e["type"] != "text_delta" and "cursor" in e]
        replayed = [e for e in link.events(after=0, limit=1000)["events"] if e["runId"] == run_id]
        case.equal(sorted(journaled(run.events) + journaled(follow.events)), journaled(replayed),
                   "journal events seen live plus those followed, against the full replay")
        case.check(FINAL in str(link.history().get("history")), "the detached run did not commit its history")
        check_effects(case, [run, follow], tags, complete=True)
    else:
        recorded = wait_until(lambda: _recorded_end(link, run_id), what="the abandoned run to end")
        if after < final_turn:
            case.equal(recorded.get("status"), "aborted", "status of a run whose client disconnected")
            case.equal(link.history().get("history"), [], "history after a disconnect-cancelled run")
        case.note("~outcome", recorded.get("status"))
        check_effects(case, [run], tags)
    committed = bool(link.history().get("history"))
    settle(case, link, tags, committed=committed)


def specs(target: Any, options: Any) -> list[Spec]:
    measured = baseline(target, "http")
    if measured.error:
        return broken("client", "http", measured)
    return [Spec(f"client[{'detach' if detach else 'attached'},after={index}]", "client",
                 lambda case, i=index, d=detach, f=measured.final_turn: disconnect(case, i, d, f),
                 variant="http", think=THINK, quick=index % 2 == 1)
            for detach in (False, True) for index in range(1, measured.events)]
