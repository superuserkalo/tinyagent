"""Sweep b: every provider misbehaviour, injected at every model request of the conversation.

The run must complete (after a retry where the specification says so) or end
with a named error; it must never hang; and the session must take another
prompt afterwards.
"""

from __future__ import annotations

from typing import Any

import invariants
from fake_provider import FAULTS, Fault
from harness import FINAL, Case, Spec
from sweeps.common import check_effects, settle
from wire import WIRES

REQUESTS = 4        # model requests in the standard conversation


def provider_fault(case: Case, fault: str, at: int) -> None:
    expect = FAULTS[fault]
    link = case.link()
    prompt, tags = case.standard(slow=0.05)
    case.provider.fault(at, Fault.named(fault))
    run = link.prompt(prompt).drain()
    status = run.status
    case.check(run.end is not None, f"the run never reached a terminal event: {status}{case.stderr_tail()}")
    seen = len(case.provider.requests)
    if expect["expect"] == "completed":
        case.check(status == "completed", f"expected the run to complete, got {status}: {run.end.get('error')}")
        case.equal(run.end.get("text"), FINAL, "final text")
        extra = 1 if expect["retried"] or fault == "unknown_tool" else 0
        case.equal(seen, REQUESTS + extra, "model requests (a retried request is sent exactly once more)")
        if expect["retried"]:
            case.equal(run.end.get("usage", {}).get("requests"), REQUESTS,
                       "usage.requests (F10: counts requests that returned a response)")
        check_effects(case, [run], tags, complete=True)
    else:
        case.check(status == "completed" or status.startswith("failed:"), f"expected completed or failed, got {status}")
        if status != "completed":
            error = run.end.get("error") or {}
            case.check(isinstance(error.get("code"), str) and error["code"] and isinstance(error.get("message"), str),
                       f"a failed run must name its error: {error}")
            case.check(isinstance(error.get("retryable"), bool), f"error.retryable must be a boolean: {error}")
        if expect["expect"] == "failed":
            case.check(status.startswith("failed:"), f"expected the run to fail, got {status}")
            case.equal(seen, at, "model requests (this failure must not be retried)")
        else:
            case.note("outcome", status)
        check_effects(case, [run], tags)
    committed = status == "completed"
    case.hold(invariants.agreement(link, run, messages_before=0))
    settle(case, link, tags, committed=committed)


def specs(target: Any, options: Any) -> list[Spec]:
    out = []
    for wire in WIRES:
        for fault in FAULTS:
            for at in range(1, REQUESTS + 1):
                quick = wire == "openai" or at == 2
                out.append(Spec(f"provider[{wire},{fault},request={at}]", "provider",
                                lambda case, f=fault, a=at: provider_fault(case, f, a),
                                wire=wire, variant="rpc" if at % 2 else "http", quick=quick))
    return out
