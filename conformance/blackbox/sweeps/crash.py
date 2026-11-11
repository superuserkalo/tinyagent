"""Sweep a: SIGKILL at every journal record, and at seeded random moments.

For each k the standard conversation runs until the session journal holds k
records, the process is killed, and the stored session is reopened and either
resumed or abandoned.
"""

from __future__ import annotations

import random
import time
from typing import Any

import invariants
from driver import CommandError, TargetDied
from harness import Case, Skip, Spec
from sweeps.common import SLOW, THINK, baseline, broken, check_effects, finished, settle


def _interrupt(case: Case, at: int | None, delay: float) -> tuple[Any, dict[str, str], list]:
    """Start the conversation and kill the target at record `at`, or after `delay` seconds."""
    prompt, tags = case.standard(SLOW)
    runs: list = []
    watcher = None
    try:
        if case.variant == "http":
            process = case.serve().process
            watcher = case.kill_at(process, at) if at else None
            link = case.http()
        else:
            link = case.rpc()
            process = link.process
            watcher = case.kill_at(process, at) if at else None
        run = link.prompt(prompt)
        runs.append(run)
        if at is None:
            time.sleep(delay)        # the one real sleep: the random moment itself
            process.kill()
        run.drain()
    except (TargetDied, CommandError):
        pass                         # killed before the prompt was accepted
    if watcher is not None and not watcher.finish():
        raise Skip(f"the journal never reached {at} records")
    process.wait(5)
    return process, tags, runs


def crash(case: Case, at: int | None, way_out: str, delay: float = 0.0) -> None:
    _, tags, runs = _interrupt(case, at, delay)
    case.wait_children()
    case.note("~records at kill", case.records())
    if not case.session_ids():
        return case.hold(invariants.usable(case, case.link()))      # died before the session existed
    link = case.reopen_stored()
    state = link.state()
    case.note("~after restart", "recoveryRequired" if state.get("recoveryRequired") else
              "resumable" if state.get("resumable") else "idle")
    case.check(not state.get("recoveryRequired"), "the CLI default is to tell the model, not to require reconcile")
    committed = False
    if state.get("resumable"):
        if way_out == "resume":
            resumed = link.resume().drain()
            runs.append(resumed)
            finished(case, resumed)
            case.check(resumed.events[0].get("resumed") is True, "agent_start of a resumed run lacks resumed:true")
            for start in resumed.of("tool_execution_start"):
                end = next(e for e in resumed.of("tool_execution_end") if e["toolCallId"] == start["toolCallId"])
                case.check(not start.get("interrupted") or end.get("isError"),
                           "a call settled as interrupted reported success")
            check_effects(case, runs, tags)
            case.hold(invariants.agreement(link, resumed))
            committed = True
        else:
            case.equal(link.abandon("sweep").get("abandoned"), True, "abandon reply")
            case.check(not link.state().get("resumable"), "still resumable after abandon")
    else:
        history = link.history().get("history") or []
        committed = bool(history)
        if runs and runs[0].status == "completed":
            case.check(committed, "a run reported completed was lost by the crash")
    check_effects(case, runs, tags)
    settle(case, link, tags, way_out, committed)


def specs(target: Any, options: Any) -> list[Spec]:
    out: list[Spec] = []
    for transport in ("rpc", "http"):
        measured = baseline(target, transport)
        if measured.error:
            out += broken("crash", transport, measured)
            continue
        for k in range(1, measured.records + 1):
            for way_out in ("resume", "abandon"):
                quick = transport == "rpc" and (way_out == "resume" or k % 3 == 0)
                out.append(Spec(f"crash[{transport},k={k},{way_out}]", "crash",
                                lambda case, k=k, w=way_out: crash(case, k, w),
                                variant=transport, think=THINK, quick=quick))
        count = 6 if options.quick else 30
        for index in range(count):
            delay = random.Random(options.seed * 1000 + index).uniform(0, measured.seconds)
            way_out = "resume" if index % 2 == 0 else "abandon"
            out.append(Spec(f"crash[{transport},random={index},{way_out}]", "crash",
                            lambda case, d=delay, w=way_out: crash(case, None, w, d),
                            variant=transport, think=THINK, quick=transport == "rpc"))
    return out
