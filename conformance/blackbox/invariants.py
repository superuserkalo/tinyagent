"""The invariants every scenario and sweep step is held to. Each returns a list of violations.

1. stream     every tool_execution_start has exactly one matching end
2. requests   every request the provider received was well-formed
3. effects    a command's visible effect happened at most once (exactly once if it reported success)
4. agreement  a run reached one terminal state and state / history / events agree with it
5. deadlines  enforced by driver.Hang, not here
6. replay     cursors strictly increase and paging never repeats or drops an event
7. usable     after any failure the session takes a new prompt, or offers a working way out
8. processes  nothing is left running (Case.finish)
"""

from __future__ import annotations

import json
from typing import Any

from driver import TERMINAL, CommandError, Run


def stream(events: list[dict], complete: bool = True) -> list[str]:
    """Invariants 1 and 6 on one live stream."""
    found: list[str] = []
    if not events:
        return ["empty event stream"] if complete else []
    if events[0].get("type") not in ("agent_start", "compaction_start"):
        found.append(f"stream starts with {events[0].get('type')}")
    terminals = [index for index, event in enumerate(events) if event.get("type") in TERMINAL]
    if complete and terminals != [len(events) - 1]:
        found.append(f"expected exactly one terminal event, last; got terminals at {terminals} of {len(events)}")
    if any("trailingBytes" in event for event in events):
        found.append("the response went on after its terminal event")
    if len({event.get("runId") for event in events}) != 1:
        found.append("events of one run carry different runIds")
    sequences = [event.get("sequence") for event in events]
    if sequences[0] != 1 or any(not isinstance(b, int) or b <= a for a, b in zip(sequences, sequences[1:])):
        found.append(f"sequence numbers are not strictly increasing from 1: {sequences}")
    cursors = [event["cursor"] for event in events if "cursor" in event]
    if any(b <= a for a, b in zip(cursors, cursors[1:])):
        found.append(f"cursors are not strictly increasing: {cursors}")
    running: str | None = None
    ended: set[str] = set()
    for event in events:
        call = event.get("toolCallId")
        if event.get("type") == "tool_execution_start":
            if running is not None:
                found.append(f"tool {call} started while {running} was still running")
            if call in ended:
                found.append(f"tool {call} started twice")
            running = call
        elif event.get("type") == "tool_execution_end":
            if running != call:
                found.append(f"tool_execution_end for {call} without a matching start")
            running = None
            ended.add(call)
    if complete and running is not None:
        found.append(f"tool_execution_start for {running} has no end")
    return found


def requests(provider: Any) -> list[str]:
    """Invariant 2."""
    return [f"request {request.number} ({request.wire}) malformed: {problem}"
            for request in provider.requests for problem in request.problems]


def effects(counts: dict[str, int], allowed: dict[str, int], required: set[str] = frozenset()) -> list[str]:
    """Invariant 3. `allowed` maps each tag to how often it may have run (normally 1)."""
    found = [f"command '{tag}' ran {counts[tag]} times, allowed {limit}"
             for tag, limit in allowed.items() if counts.get(tag, 0) > limit]
    found += [f"command '{tag}' reported success but its effect is missing" for tag in required
              if counts.get(tag, 0) < 1]
    return found


def succeeded_tags(runs: list[Run], tags_by_call: dict[str, str]) -> set[str]:
    """Tags of commands whose tool_execution_end said isError false in any stream."""
    return {tags_by_call[event["toolCallId"]] for run in runs for event in run.of("tool_execution_end")
            if not event.get("isError") and event.get("toolCallId") in tags_by_call}


def all_events(link: Any, page: int = 7) -> tuple[list[dict], list[str]]:
    """Every replayable event, read in small pages. Invariant 6 on replay."""
    found: list[str] = []
    events: list[dict] = []
    after = 0
    for _ in range(10_000):
        reply = link.events(after=after, limit=page)
        batch = reply.get("events")
        if not isinstance(batch, list):
            return events, found + [f"get_events returned no events list: {str(reply)[:120]}"]
        if len(batch) > page:
            found.append(f"get_events returned {len(batch)} events for limit {page}")
        events += batch
        following = reply.get("nextAfter")
        if following is None:
            break
        if not batch or following != batch[-1].get("cursor"):
            found.append(f"nextAfter {following} is not the last cursor of its page")
            break
        after = following
    cursors = [event.get("cursor") for event in events]
    if any(not isinstance(cursor, int) or cursor >= 2 ** 53 for cursor in cursors):
        found.append(f"replayed events without a valid cursor: {cursors}")
    elif any(b <= a for a, b in zip(cursors, cursors[1:])):
        found.append(f"replayed cursors are not strictly increasing: {cursors}")
    whole = link.events(after=0, limit=1000).get("events") or []
    if [event.get("cursor") for event in whole] != cursors:
        found.append("paged replay differs from a single-page replay")
    return events, found


def replay(link: Any) -> list[str]:
    """Invariants 1 and 6 on the journal-derived events of every finished run."""
    events, found = all_events(link)
    by_run: dict[str, list[dict]] = {}
    for event in events:
        by_run.setdefault(event.get("runId"), []).append(event)
    for run_id, group in by_run.items():
        ends = [event for event in group if event.get("type") == "agent_end"]
        if len(ends) > 1:
            found.append(f"run {run_id} has {len(ends)} agent_end events in replay")
        starts = [e.get("toolCallId") for e in group if e.get("type") == "tool_execution_start"]
        finishes = [e.get("toolCallId") for e in group if e.get("type") == "tool_execution_end"]
        if len(set(starts)) != len(starts) or len(set(finishes)) != len(finishes):
            found.append(f"run {run_id} replays a tool event twice")
        if ends and ends[0].get("status") != "paused" and set(starts) - set(finishes):
            found.append(f"finished run {run_id} replays a tool start with no end: {set(starts) - set(finishes)}")
        if set(finishes) - set(starts):
            found.append(f"run {run_id} replays a tool end with no start: {set(finishes) - set(starts)}")
    return found


def agreement(link: Any, run: Run, messages_before: int | None = None, journal_wins: bool = False) -> list[str]:
    """Invariant 4: state, history and events agree with the terminal event the client saw.

    `journal_wins` applies ruling F17 where the client cancelled or disconnected:
    a live "completed" may be recorded as failed or aborted, and then the
    journal's status is the one history must agree with.
    """
    found: list[str] = []
    end = run.end
    if end is None:
        return [f"run did not reach a terminal state: {run.status}"]
    if journal_wins and end.get("status") == "completed":
        recorded = [event for event in link.events(after=0, limit=1000).get("events") or []
                    if event.get("runId") == end.get("runId") and event.get("type") == "agent_end"]
        if recorded and recorded[-1].get("status") != "completed":
            end = recorded[-1]
    state = link.state()
    if state.get("running"):
        found.append("get_state says running after agent_end")
    if link.transport == "rpc" and state.get("runId") is not None:
        found.append("get_state still names a runId after agent_end")
    if end.get("status") != "paused" and state.get("resumable"):
        found.append(f"session is resumable after a run ended {end.get('status')}")
    history = link.history().get("history")
    if not isinstance(history, list):
        return found + ["get_history returned no history list"]
    if end.get("status") == "completed":
        if end.get("text") and end["text"] not in json.dumps(history, ensure_ascii=False):
            found.append("final text of a completed run is not in committed history")
    elif messages_before is not None and len(history) != messages_before:
        found.append(f"a run that ended {end.get('status')} changed committed history "
                     f"({messages_before} -> {len(history)} messages)")
    if link.transport == "rpc" and state.get("messageCount") != len(history):
        found.append(f"messageCount {state.get('messageCount')} differs from history length {len(history)}")
    if end.get("repeated"):
        return found
    events, problems = all_events(link, page=50)
    found += problems
    ends = [event for event in events if event.get("runId") == end.get("runId") and event.get("type") == "agent_end"]
    if len(ends) != 1:
        found.append(f"replay has {len(ends)} agent_end events for run {end.get('runId')} (live status {run.status})")
    elif journal_wins:
        pass
    elif ends[0].get("status") != end.get("status") or \
            (ends[0].get("error") or {}).get("code") != (end.get("error") or {}).get("code"):
        found.append(f"replay says run ended {ends[0].get('status')} {ends[0].get('error')}, "
                     f"the live stream said {run.status}")
    return found


def usable(case: Any, link: Any, way_out: str = "abandon") -> list[str]:
    """Invariant 7. Takes the session out of any open state, then runs one more prompt."""
    found: list[str] = []
    state = link.state()
    if state.get("recoveryRequired"):
        link.reconcile("checked by the harness")
        state = link.state()
    if state.get("resumable") or state.get("paused"):
        try:
            if way_out == "resume":
                link.resume().drain()
            else:
                link.abandon("harness")
        except CommandError as error:
            found.append(f"session reports resumable but {way_out} failed: {error}")
        if link.state().get("resumable"):
            found.append(f"session is still resumable after {way_out}")
    try:
        run = link.prompt(case.script(_say("still alive"))).drain()
    except CommandError as error:
        return found + [f"session does not accept a new prompt: {error}"]
    if run.status != "completed":
        found.append(f"follow-up prompt ended {run.status}, expected completed")
    return found


def no_trace(case: Any, call_ids: Any) -> list[str]:
    """F6/F20: the newest model request carries nothing of a failed, aborted or abandoned run."""
    request = case.provider.requests[-1]
    leaked = {item["id"] for item in request.items if item["kind"] in ("call", "result")} & set(call_ids)
    return [f"request {request.number} carries tool calls of a run that did not commit: {sorted(leaked)}"] if leaked else []


def _say(text: str) -> Any:
    from wire import say
    return say(text)
