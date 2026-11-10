"""Public RPC cancellation, invalid options, and owner-process cleanup."""

import os
import sys

from rpc_client import Client


def main():
    mode, beam, jason = sys.argv[1:]
    client = Client(
        beam,
        jason,
        control=mode != "invalid",
        fixture="rpc_api_fixture.exs",
        arguments=[mode],
    )
    try:
        if mode == "invalid":
            assert client.finish() == []
            assert client.stderr == ["invalid checked"], client.stderr
        else:
            client.prompt("wait")
            for _ in range(3):
                client.value()
            if mode == "owner_exit":
                assert client.line("stderr") == b"owner ready"
            os.write(client.control, b"x")
            client.process.wait(timeout=3)
            values = client.finish()
            if mode == "external_cancel":
                assert len(values) == 1 and values[0]["status"] == "aborted", values
                assert client.stderr == ["provider cleaned", "cancelled"], client.stderr
            else:
                assert client.stderr == [
                    "owner ready",
                    "provider cleaned",
                    "owner cleaned",
                ], client.stderr
        print(mode + " passed")
    finally:
        client.close()


if __name__ == "__main__":
    main()
