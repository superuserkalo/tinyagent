"""What the sweeps share: the baseline measurement and the checks after every step."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import invariants
from driver import Run
from harness import FINAL, Case, Failure, Spec

SLOW = 0.3          # seconds the slow command of the standard conversation sleeps
THINK = 0.04        # model latency, so a kill or cancel lands between two steps


@dataclass
class Baseline:
    records: int        # journal records the standard conversation writes
    events: int         # events in its live stream
    final_turn: int     # index of the first event of the last model turn
    seconds: float
    error: str = ""


_cache: dict[tuple, Baseline] = {}


def baseline(target: Any, transport: str, wire: str = "openai") -> Baseline:
    """Run the standard conversation once, undisturbed, and measure it."""
    key = (target.name, transport, wire)
    if key not in _cache:
        case = Case(target, wire, transport, THINK)
        try:
            link = case.link()
            prompt, _ = case.standard(SLOW)
            began = time.monotonic()
            run = link.prompt(prompt).drain()
            seconds = time.monotonic() - began
            types = [event["type"] for event in run.events]
            last_tool = max((i for i, kind in enumerate(types) if kind == "tool_execution_end"), default=0)
            problems = [] if run.status == "completed" else [f"baseline run ended {run.status}{case.stderr_tail()}"]
            _cache[key] = Baseline(case.records(), len(types), last_tool + 1, seconds, "; ".join(problems + case.finish()))
        except Exception as error:      # reported as one failing case by the sweep
            _cache[key] = Baseline(0, 0, 0, 0.0, f"{type(error).__name__}: {error}")
        finally:
            case.cleanup()
    return _cache[key]


def broken(group: str, label: str, measured: Baseline) -> list[Spec]:
    def fail(case: Case) -> None:
        raise Failure(f"the undisturbed standard conversation failed, so nothing was swept: {measured.error}")
    return [Spec(f"{group}[{label},baseline]", group, fail)]


def check_effects(case: Case, runs: list[Run], tags: dict[str, str], complete: bool = False) -> None:
    """Invariant 3. With `complete`, every command must have run exactly once."""
    case.wait_children()
    required = set(tags.values()) if complete else invariants.succeeded_tags(runs, tags)
    case.hold(invariants.effects(case.effects(), {tag: 1 for tag in tags.values()}, required))


def settle(case: Case, link: Any, tags: dict[str, str], way_out: str = "abandon", committed: bool = False) -> None:
    """Invariants 6 and 7 after a step, and F20 when the swept run did not commit."""
    case.hold(invariants.replay(link))
    case.hold(invariants.usable(case, link, way_out))
    if not committed:
        case.hold(invariants.no_trace(case, tags))


def finished(case: Case, run: Run) -> None:
    case.completed(run)
    case.equal(run.end.get("text"), FINAL, "final text")
