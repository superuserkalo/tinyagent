"""Output limits and cancellation while native Elixir RPC pipes are stalled."""

import json
import os
import selectors
import sys
import threading
import time

from rpc_client import Client, LIMIT


def large_state(client, data, size=LIMIT):
    response = {
        "type": "response",
        "command": "get_state",
        "success": True,
        "id": "",
        "data": data,
    }
    overhead = len(json.dumps(response, separators=(",", ":")).encode())
    command = {"type": "get_state", "id": "x" * (size - overhead)}
    client.send(command)


def check(mode, client):
    if mode == "output_limit":
        large_state(client, client.state())
        line = client.line()
        assert len(line) == LIMIT
        assert json.loads(line)["data"]["status"] == "idle"
        client.finish()
    elif mode == "oversized_ack":
        # This input fits, but its acknowledgement needs more JSON envelope bytes.
        prefix = b'{"type":"prompt","message":"go","id":"'
        suffix = b'"}\n'
        client.send_bytes(prefix + b"x" * (LIMIT - len(prefix) - len(suffix)) + suffix)
        assert client.value()["error"]["code"] == "output_too_large"
        assert client.idle()["messageCount"] == 0
        client.finish()
        assert not client.stderr, client.stderr
    elif mode == "oversized_reply":
        state = client.state()
        # Oversized replies cancel the current run before accepting another command.
        large_state(client, state, LIMIT + 1)
        client.prompt("go")
        assert client.value()["error"]["code"] == "output_too_large"
        assert client.terminal()[-1]["status"] == "completed"
        assert client.idle()["messageCount"] == 2
        client.prompt("wait")
        for _ in range(3):
            client.value()
        state = client.state()
        large_state(client, state, LIMIT + 1)
        values = client.terminal()
        assert values[0]["error"]["code"] == "output_too_large"
        assert values[-1]["status"] == "aborted"
        assert client.idle()["messageCount"] == 2
        client.finish()
        assert "cleanup:wait" in client.stderr
    elif mode == "stalled_eof":
        client.prompt("delta")
        assert client.value()["command"] == "prompt"
        assert client.value()["type"] == "agent_start"
        assert client.line("stderr") == b"provider:delta:1"
        client.process.stdin.close()
        client.process.wait(timeout=3)
        partial = client.finish(status=2, complete_lines=False)
        assert partial and not partial.endswith(b"\n")
        assert "cleanup:delta" in client.stderr
        assert "error:output_stalled" in client.stderr
    elif mode == "broken_pipe":
        client.prompt("wait")
        for _ in range(3):
            client.value()
        client.process.stdout.close()
        client.send({"type": "get_state"})
        client.process.wait(timeout=3)
        client.finish(status=2)
        assert "cleanup:wait" in client.stderr
        assert "error:output_error" in client.stderr
    elif mode == "queue_limit":
        client.prompt("wait")
        for _ in range(3):
            client.value()
        state = client.state()
        assert state["status"] == "running"
        assert client.line("stderr") == b"provider:wait:1"
        errors = []

        def flood():
            try:
                # Four complete lines plus LF exceed 4 MiB by exactly four bytes.
                # The first line remains counted in full during partial writes.
                for _ in range(4):
                    large_state(client, state)
            except (BrokenPipeError, OSError, ValueError) as error:
                errors.append(error)

        sender = threading.Thread(target=flood, daemon=True)
        sender.start()
        assert client.line("stderr") == b"cleanup:wait"
        values = client.finish(status=2)
        sender.join(timeout=3)
        assert not sender.is_alive()
        assert not errors, errors
        assert len(values) == 2, [v["type"] for v in values]
        assert values[0]["data"] == state
        assert values[1]["error"]["code"] == "output_queue_full"
        assert "error:output_queue_full" in client.stderr
    elif mode == "abort_terminal":
        client.prompt("terminal")
        assert client.value()["command"] == "prompt"
        assert client.value()["type"] == "agent_start"
        assert client.value()["delta"] == "preparing"
        assert client.line("stderr") == b"provider:terminal:1"
        assert client.line("stderr") == b"cleanup:terminal"
        # Provider cleanup precedes Agent's completion check. Wait for actual
        # terminal bytes so cancellation targets delivery, not that earlier check.
        if not client.buffers["stdout"]:
            with selectors.DefaultSelector() as selector:
                selector.register(client.process.stdout, selectors.EVENT_READ)
                assert selector.select(5), "Terminal output did not begin"
            prefix = os.read(client.process.stdout.fileno(), 1)
            assert prefix, "Output closed before the terminal frame"
            client.buffers["stdout"] += prefix
        assert b"\n" not in client.buffers["stdout"]
        client.send({"type": "abort", "id": "abort"})
        # Keep the large terminal frame held while the reader accepts cancellation.
        time.sleep(0.1)
        values = [client.value(), client.value()]
        response = next(v for v in values if v.get("id") == "abort")
        assert response["data"]["accepted"]
        terminal = next(v for v in values if v["type"] == "agent_end")
        assert terminal["text"] == "x" * 262_144
        assert client.idle()["messageCount"] == 0
        client.prompt("go")
        client.terminal()
        assert client.idle()["messageCount"] == 2
        client.finish()
    elif mode == "abort_pending_ack":
        large_state(client, client.state())
        client.prompt("go")
        client.send({"type": "abort", "id": "abort"})
        time.sleep(0.1)
        assert client.value()["command"] == "get_state"
        values = [client.value(), client.value()]
        assert next(v for v in values if v.get("id") == "abort")["data"]["accepted"]
        assert next(v for v in values if v.get("id") == "prompt")["success"]
        assert client.idle()["messageCount"] == 0
        assert client.finish() == []
        assert not client.stderr, client.stderr
    elif mode == "eof_cleanup":
        client.prompt("held")
        for _ in range(3):
            client.value()
        assert client.line("stderr") == b"provider:held:1"
        client.process.stdin.close()
        assert client.line("stderr") == b"cleanup:held"
        time.sleep(0.05)
        assert client.process.poll() is None
        os.write(client.control, b"x")
        values = client.finish()
        assert len(values) == 1 and values[0]["status"] == "aborted", values
    else:
        raise AssertionError(mode)


def main():
    mode, beam, jason = sys.argv[1:]
    client = Client(beam, jason, control=mode == "eof_cleanup")
    try:
        check(mode, client)
        print(mode + " passed")
    finally:
        client.close()


if __name__ == "__main__":
    main()
