"""Sweep e: a seeded random walk over one session.

Each step is chosen from what the session's state allows: prompt, a repeated
messageId, abort mid-run, SIGKILL mid-run, clean restart, resume, abandon,
compact, get_history, get_events. Invariants are checked after every step. A
failure names the step; the same seed replays the same walk.
"""

from __future__ import annotations

import random
from typing import Any

import invariants
from harness import Case, Failure, Spec
from wire import bash, say

BUDGET = {"TINYAGENT_CONTEXT_TOKENS": "200000", "TINYAGENT_RESPONSE_RESERVE": "1000"}


class Walk:
    def __init__(self, case: Case, rng: random.Random) -> None:
        self.case, self.rng = case, rng
        self.link = case.link()
        self.link._known_id = self.link.session_id
        self.tags: dict[str, str] = {}       # tool call ID -> effect tag, for every command ever scripted
        self.committed: list[tuple[str, str]] = []      # (messageId, final text) of committed prompts
        self.count = 0

    def script(self, slow: float = 0.0) -> tuple[str, str, str]:
        """A fresh two-command conversation: (prompt, messageId, final text)."""
        self.count += 1
        first, second = f"w{self.count}a", f"w{self.count}b"
        turns = [bash(self.case.command(first, slow)), bash(self.case.command(second)), say(f"done {self.count}")]
        prompt = self.case.script(*turns)
        self.tags.update({turns[0].calls[0].id: first, turns[1].calls[0].id: second})
        return prompt, f"m{self.count}", f"done {self.count}"

    # -- steps ----------------------------------------------------------------

    def prompt(self) -> str:
        prompt, message_id, text = self.script()
        run = self.case.completed(self.link.prompt(prompt, message_id=message_id), "prompt")
        self.case.equal(run.end.get("text"), text, "final text")
        self.committed.append((message_id, text))
        self.case.hold(invariants.agreement(self.link, run))
        return "completed"

    def duplicate(self) -> str:
        if not self.committed:
            return self.prompt()
        message_id, text = self.rng.choice(self.committed)
        before = len(self.case.provider.requests)
        run = self.link.prompt("[[none]] the same message again", message_id=message_id).drain()
        self.case.equal(run.status, "completed", "status of a repeated messageId")
        self.case.check(run.end.get("repeated") is True, "agent_end of a repeated messageId lacks repeated:true")
        self.case.equal(run.end.get("text"), text, "stored text returned for a repeated messageId")
        self.case.equal(len(self.case.provider.requests), before, "model requests made for a repeated messageId")
        return "repeated"

    def abort(self) -> str:
        prompt, message_id, _ = self.script(slow=0.2)
        run = self.link.prompt(prompt, message_id=message_id).take(self.rng.randint(1, 4))
        self.link.abort()
        run.drain()
        self.case.check(run.status in ("aborted", "completed"), f"an aborted run ended {run.status}")
        self.case.hold(invariants.agreement(self.link, run, journal_wins=True))
        return run.status

    def kill(self) -> str:
        prompt, message_id, _ = self.script(slow=0.2)
        run = self.link.prompt(prompt, message_id=message_id).take(self.rng.randint(1, 5))
        self.link.kill()
        run.drain()
        self.case.wait_children()
        self.link = self.case.reopen(self.link)
        return "killed"

    def restart(self) -> str:
        self.link = self.case.reopen(self.link)
        return "reopened"

    def resume(self) -> str:
        run = self.case.completed(self.link.resume(), "resume")
        self.case.check(run.events[0].get("resumed") is True, "agent_start of a resumed run lacks resumed:true")
        self.case.hold(invariants.agreement(self.link, run))
        return "completed"

    def abandon(self) -> str:
        self.case.equal(self.link.abandon("walk").get("abandoned"), True, "abandon reply")
        return "abandoned"

    def compact(self) -> str:
        run = self.link.compact().drain()
        self.case.check(run.end is not None and run.end.get("status") in ("completed", "failed"),
                        f"compaction ended {run.status}")
        return f"{run.end.get('status')} changed={run.end.get('changed')}"

    def history(self) -> str:
        page = self.link.history(view="records", limit=1000)
        self.case.check(isinstance(page.get("records"), list), "get_history view=records returned no records")
        return f"{len(page['records'])} records"

    def events(self) -> str:
        events, problems = invariants.all_events(self.link, page=self.rng.randint(1, 9))
        self.case.hold(problems)
        return f"{len(events)} events"

    # -- the walk ---------------------------------------------------------------

    def step(self) -> tuple[str, str]:
        state = self.link.state()
        self.case.check(not state.get("recoveryRequired"), "the CLI must not require reconcile by default")
        if state.get("resumable"):
            choices = ["resume", "resume", "abandon", "abandon", "restart", "history", "events"]
        else:
            choices = ["prompt", "prompt", "duplicate", "abort", "kill", "kill", "compact", "restart",
                       "history", "events"]
        action = self.rng.choice(choices)
        outcome = getattr(self, action)()
        self.case.wait_children()
        self.case.hold(invariants.effects(self.case.effects(), {tag: 1 for tag in self.tags.values()},
                                          invariants.succeeded_tags(self.case.runs, self.tags)))
        self.case.hold(invariants.replay(self.link))
        self.case.hold(invariants.requests(self.case.provider))
        return action, outcome


def walk(case: Case, seed: int, steps: int) -> None:
    case.configure(**BUDGET)
    walker = Walk(case, random.Random(seed))
    log: list[str] = []
    for number in range(1, steps + 1):
        try:
            action, outcome = walker.step()
        except Exception as error:
            raise Failure(f"step {number} failed: {type(error).__name__}: {error}\n"
                          f"    steps before it: {' | '.join(log) or 'none'}") from error
        log.append(f"{number} {action}: {outcome}")
    case.note("~steps", " | ".join(log))
    case.hold(invariants.usable(case, walker.link))


def specs(target: Any, options: Any) -> list[Spec]:
    count, steps = (3, 12) if options.quick else (12, 30)
    return [Spec(f"walk[{transport},seed={options.seed * 100 + index}]", "walk",
                 lambda case, s=options.seed * 100 + index: walk(case, s, steps), variant=transport)
            for transport in ("rpc", "http") for index in range(count)]
