"""The one-shot and interactive CLI: exit codes, approval, limits, resume."""

from __future__ import annotations

from fake_provider import Fault
from harness import FINAL, Case, scenario
from wire import bash, say


def _results(case: Case) -> list[str]:
    return [item["text"] for item in case.provider.requests[-1].items if item["kind"] == "result"]


@scenario("D21, A7: one-shot exit codes and output")
def oneshot_exit_codes(case: Case) -> None:
    code, out, _ = case.oneshot("-p", case.script(bash(case.command("x"), text="Working. "), say(FINAL)), "--yolo")
    case.equal(code, 0, "exit code of a completed run")
    case.check("Working. " in out and FINAL in out, f"streamed text on stdout: {out!r}")
    case.equal(case.effects().get("x"), 1, "command effect with --yolo")
    code, out, _ = case.oneshot("--prompt", case.script(say("long form")), env={"TINYAGENT_YOLO": "1"})
    case.check(code == 0 and "long form" in out, "--prompt and TINYAGENT_YOLO=1")
    case.provider.fault_next(Fault("status", 401))
    code, _, err = case.oneshot("-p", case.script(say("never")), "--yolo")
    case.equal(code, 1, "exit code of a failed run")
    case.check(err.strip(), "a failed one-shot run should say why on stderr")
    code, _, _ = case.oneshot("-p", case.script(bash("true"), bash("true"), say("never")), "--yolo", "--max-steps", "1")
    case.equal(code, 1, "exit code when --max-steps ends the run")
    case.check(case.session_ids(), "one-shot runs are stored in TINYAGENT_SESSION_DIR")
    code, out, _ = case.oneshot("--list-sessions")
    case.equal(sorted(out.split()), case.session_ids(), "--list-sessions")


@scenario("D20, A8: approval in one-shot mode")
def oneshot_approval(case: Case) -> None:
    mark = lambda name: str(case.root / name)
    script = lambda *commands: case.script(*[bash(command) for command in commands], say(FINAL))
    code, _, _ = case.oneshot("-p", script(f"touch {mark('plain')}"))
    case.check(not (case.root / "plain").exists(), "one-shot without --yolo or --allow ran a command")
    case.equal(code, 0, "exit code: the rejection is a tool error the model recovers from")
    case.check("--yolo" in _results(case)[0], f"the rejection should say how to allow it: {_results(case)[0]!r}")
    case.oneshot("-p", script(f"touch {mark('allowed')}", f"touch {mark('a')}; touch {mark('b')}",
                              f"ls {case.root}", f"touch {mark('c')} extra"),
                 "--allow", f"touch {case.root}/[a-z]+", "--allow", "pwd")
    case.check((case.root / "allowed").exists(), "--allow did not approve a command it matches in full")
    for name in ("a", "b", "c"):
        case.check(not (case.root / name).exists(), f"--allow approved a command it must not ({name})")
    case.check(all("Rejected" in text or "rejected" in text or "--yolo" in text for text in _results(case)[1:]),
               f"rejections seen by the model: {_results(case)[1:]}")
    case.oneshot("-p", script("git reset --hard HEAD", f"touch {mark('yolo')}"), "--yolo")
    case.check((case.root / "yolo").exists(), "--yolo did not run the command")
    case.check("denied" in _results(case)[0] or "blocked" in _results(case)[0],
               f"the destructive-command list must still apply with --yolo: {_results(case)[0]!r}")


@scenario("D20, A8: the interactive prompt (y, a reason, a)")
def interactive_approval(case: Case) -> None:
    mark = lambda name: str(case.root / name)
    prompt = case.script(bash(f"touch {mark('one')}"), bash(f"touch {mark('two')}"), bash(f"touch {mark('three')}"),
                         bash(f"touch {mark('four')}"), say(FINAL))
    code, out, err = case.oneshot(stdin=f"{prompt}\ny\ntoo risky for me\na\n", mode="interactive")
    case.equal(code, 0, "exit code at end of input")
    case.equal([(case.root / name).exists() for name in ("one", "two", "three", "four")], [True, False, True, True],
               "commands run after answering y, a reason, a")
    results = _results(case)
    case.check(len(results) == 4, f"the run stopped early: {len(results)} tool results; stderr {err[-200:]!r}")
    case.equal(results[1], "Rejected by user: too risky for me", "what the model saw for the rejected command")


@scenario("F15: --resume continues an open run first")
def oneshot_resume(case: Case) -> None:
    link = case.rpc()
    run = link.prompt(case.script(bash(case.blocker("x", 0.4)), bash(case.command("y")), say("first done")))
    case.wait_started("x")
    link.kill()
    run.drain()
    case.wait_children()
    identifier = case.session_ids()[0]
    code, out, err = case.oneshot("-p", case.script(say("second done")), "--yolo", "--resume", identifier)
    case.check(code == 0, f"exit code {code}; stderr {err[-300:]!r}")
    case.check("first done" in out and "second done" in out, f"both the resumed run and the new prompt: {out!r}")
    case.equal(dict(case.effects()), {"x": 1, "y": 1}, "effects")
    stored = case.rpc(resume=identifier)
    case.equal(len(stored.history()["history"]), 8, "history after resume and the new prompt")
