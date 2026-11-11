"""The HTTP server: routes, status codes, authentication, detached runs and follow."""

from __future__ import annotations

import signal

import invariants
from driver import CommandError, wait_until
from harness import FINAL, Case, scenario
from wire import bash, say


def _status(case: Case, server, expected: int, method: str, path: str, what: str, **options) -> dict:
    status, body = server.request(method, path, **options)
    case.equal(status, expected, f"HTTP status for {what}")
    if expected >= 400:
        error = body.get("error") if isinstance(body, dict) else None
        case.check(isinstance(error, dict) and isinstance(error.get("code"), str), f"{what}: error body {body!r}")
        return error
    return body


@scenario("D24 v1#11, PROTOCOL endpoints and status codes, C session page")
def http_routes_and_status_codes(case: Case) -> None:
    server = case.serve()
    case.equal(server.call("GET", "/health"), {"status": "ok", "protocolVersion": 2}, "/health")
    status, created = server.request("POST", "/sessions")
    case.check(status == 201 and created["sessionId"].startswith("s_"), f"POST /sessions: {status} {created}")
    path = f"/sessions/{created['sessionId']}"
    case.check(created["sessionId"] in server.call("GET", "/sessions")["sessionIds"], "GET /sessions")
    page = server.call("GET", path)
    case.equal({key: page.get(key, "missing") for key in ("sessionId", "history", "recoveryRequired", "resumable",
                                                          "nextOffset")},
               {"sessionId": created["sessionId"], "history": [], "recoveryRequired": False, "resumable": False,
                "nextOffset": None}, "session page")
    _status(case, server, 404, "GET", "/sessions/s_missing", "unknown session")
    _status(case, server, 404, "POST", "/sessions/s_missing/prompts", "prompt to an unknown session", body={"message": "x"})
    _status(case, server, 404, "GET", "/nowhere", "unknown route")
    _status(case, server, 400, "POST", f"{path}/prompts", "malformed JSON", raw=b"{nope",
            headers={"Content-Type": "application/json"})
    _status(case, server, 400, "POST", f"{path}/prompts", "empty message", body={"message": ""})
    _status(case, server, 415, "POST", f"{path}/prompts", "wrong content type", raw=b'{"message":"x"}',
            headers={"Content-Type": "text/plain"})
    _status(case, server, 413, "POST", f"{path}/prompts", "oversized body",
            body={"message": "x" * 1_048_577})
    _status(case, server, 400, "GET", f"{path}?limit=0", "limit 0")
    case.equal(server.call("POST", f"{path}/cancel"), {"accepted": False, "runId": None}, "idle cancel")
    link = case.http()
    run = case.completed(link.prompt(case.script(say("hi"))))
    case.check(str(run.data["contentType"]).replace(" ", "").lower() == "application/jsonl;charset=utf-8",
               f"prompt stream content type: {run.data['contentType']}")
    case.equal(server.request("DELETE", link.path)[0], 204, "DELETE status")
    _status(case, server, 404, "GET", link.path, "deleted session")
    case.check(link.session_id in server.call("GET", "/sessions")["sessionIds"], "DELETE must keep the journal")
    reopened = case.http(link.session_id)
    case.equal(len(reopened.history()["history"]), 2, "history after reopening a deleted session")
    error = case.fails("session_busy", lambda: case.http(link.session_id), "opening an open session")
    case.equal(server.stop(), 0, "exit code after SIGTERM")


@scenario("D24 v1#11 authentication and loopback default")
def http_auth_and_exposure(case: Case) -> None:
    server = case.serve(token="s3cret-token")
    for headers, what in (({"Authorization": ""}, "no token"), ({"Authorization": "Bearer wrong"}, "wrong token"),
                          ({"Authorization": "Basic s3cret-token"}, "wrong scheme")):
        _status(case, server, 401, "POST", "/sessions", what, headers=headers)
    case.equal(server.request("POST", "/sessions")[0], 201, "status with the right token")
    case.note("/health without a token", server.request("GET", "/health", headers={"Authorization": ""})[0])
    for env, what in (({"TINYAGENT_HOST": "0.0.0.0", "TINYAGENT_PORT": "0"}, "no token"),
                      ({"TINYAGENT_HOST": "0.0.0.0", "TINYAGENT_PORT": "0", "TINYAGENT_TOKEN": "t"}, "no allow-remote"),
                      ({"TINYAGENT_HOST": "0.0.0.0", "TINYAGENT_PORT": "0", "TINYAGENT_ALLOW_REMOTE": "1"}, "no token")):
        process = case.spawn("serve", env=env)
        case.check(process.wait(10) != 0, f"a remote bind with {what} must refuse to start")


@scenario("PROTOCOL: DELETE and shutdown cancel active runs; invariant 8")
def http_delete_and_shutdown_cancel(case: Case) -> None:
    link = case.http()
    run = link.prompt(case.script(bash(case.blocker("deleted")), say("never")))
    case.wait_started("deleted")
    case.equal(link.server.request("DELETE", link.path)[0], 204, "DELETE during a run")
    case.equal(run.drain(5).status, "aborted", "stream of a run whose session was deleted")
    link = case.http()
    run = link.prompt(case.script(bash(case.blocker("stopping")), say("never")))
    case.wait_started("stopping")
    case.equal(link.server.stop(10), 0, "exit code after SIGTERM during a run")
    run.drain(5)
    case.check(run.status in ("aborted", "died"), f"stream at shutdown ended {run.status}")
    case.wait_children(3)
    case.equal(sum(case.effects().values()), 0, "effects of the cancelled commands")


@scenario("invariant 4: a finished stream means a finished run")
def http_prompt_right_after_stream_end(case: Case) -> None:
    link = case.http()
    for number in range(30):
        try:
            case.completed(link.prompt(case.script(say(f"reply {number}"))))
        except CommandError as error:
            raise AssertionError(f"prompt {number + 1}, sent as soon as the previous response ended, "
                                 f"was refused: {error}") from None
        case.equal(link.abort().get("accepted"), False, f"cancel right after response {number + 1} ended")


@scenario("D22, B7: a detached run is cancelled only by cancel")
def http_detached_cancel(case: Case) -> None:
    link = case.http()
    run = link.prompt(case.script(bash(case.blocker("detached")), say("never")), detach=True)
    case.wait_started("detached")
    run_id = run.take(1).events[0]["runId"]
    run.disconnect()
    follow = link.follow(0).take(2)
    case.equal([event["type"] for event in follow.events], ["agent_start", "tool_execution_start"],
               "a detached run is still active after its client left")
    case.equal(link.abort(), {"accepted": True, "runId": run_id}, "cancel of a detached run")
    case.equal(follow.drain(5).status, "aborted", "follow stream after cancel")
    case.wait_children(3)
    case.equal(case.effects().get("detached", 0), 0, "effect of the cancelled command")
    case.hold(invariants.replay(link) + invariants.usable(case, link))


@scenario("D23, B7, F2: follow joined mid-turn")
def http_follow_mid_turn(case: Case) -> None:
    link = case.http()
    answer = "The answer, after some thought, is forty-two."
    prompt = case.script(bash(case.command("x"), text="Working. "), say(answer))
    case.stall(at=2, mid=True)
    run = link.prompt(prompt)
    run.until("tool_execution_end")
    case.wait_stalled()
    live = run.take(len(run.events) + 2).of("text_delta")[-2:]
    follow = link.follow(0).take(6)
    kinds = [(event["type"], "cursor" in event) for event in follow.events]
    case.equal(kinds, [("agent_start", True), ("text_delta", True), ("tool_execution_start", True),
                       ("tool_execution_end", True), ("text_delta", False), ("text_delta", False)],
               "replayed events, then the buffered deltas of the turn in progress")
    case.equal([event["delta"] for event in follow.events[4:]], [event["delta"] for event in live], "buffered deltas")
    case.provider.release.set()
    case.equal(follow.drain().status, "completed", "follow stream end")
    case.completed(run)
    case.equal("".join(e["delta"] for e in follow.events if e["type"] == "text_delta" and "cursor" not in e), answer,
               "live text delivered exactly once")
    cursors = [event["cursor"] for event in follow.events if "cursor" in event]
    case.check(cursors == sorted(set(cursors)), f"follow cursors: {cursors}")
    case.equal(follow.end.get("cursor"), run.end.get("cursor"), "agent_end cursor on both streams")
    idle = link.follow(0).drain()
    case.equal([e["cursor"] for e in idle.events], [e["cursor"] for e in link.events(0, 1000)["events"]],
               "follow with no active run is the replay")
    case.equal(link.follow(run.end["cursor"]).drain().events, [], "follow after the last cursor")


@scenario("B7: the server never resumes an open run on its own")
def http_no_resume_at_startup(case: Case) -> None:
    link = case.http()
    run = link.prompt(case.script(bash(case.blocker("x", 0.4)), say(FINAL)), detach=True)
    case.wait_started("x")
    link.kill()
    run.drain()
    case.wait_children()
    before = len(case.provider.requests)
    link = case.reopen_stored("http")
    case.equal(link.server.call("GET", "/health")["status"], "ok", "restarted server")
    case.equal(link.state().get("resumable"), True, "the session page after restart")
    case.equal(len(case.provider.requests), before, "model requests made before any client asked to resume")
    case.completed(link.resume(detach=True), "resume")
    case.equal(case.effects().get("x"), 1, "effect of the command the crash orphaned")
