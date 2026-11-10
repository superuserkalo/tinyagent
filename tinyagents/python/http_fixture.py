"""Native HTTP parsers connected by bounded in-memory byte streams, without TCP."""

import http.client
import io
import json
import select
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from tinyagent_http import _Application, _Handler

_select = select.select


class Pipe:
    def __init__(self):
        self.condition = threading.Condition()
        self.data = bytearray()
        self.read_closed = False
        self.write_closed = False
        self.waiting = False
        self.write_waiting = False

    def read(self, size, peek=False):
        deadline = time.monotonic() + 3
        with self.condition:
            while not self.data and not self.write_closed and not self.read_closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("memory reader timed out")
                self.waiting = True
                try:
                    self.condition.wait(remaining)
                finally:
                    self.waiting = False
            if self.read_closed:
                raise ConnectionError("memory reader closed")
            result = bytes(self.data[:size])
            if not peek:
                del self.data[:size]
                self.condition.notify_all()
            return result

    def write(self, data):
        offset = 0
        deadline = time.monotonic() + 3
        with self.condition:
            while offset < len(data):
                if self.read_closed or self.write_closed:
                    raise BrokenPipeError("memory pipe closed")
                room = 1024 - len(self.data)
                if room:
                    count = min(room, len(data) - offset)
                    self.data.extend(data[offset : offset + count])
                    offset += count
                    self.condition.notify_all()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("memory writer timed out")
                    self.write_waiting = True
                    try:
                        self.condition.wait(remaining)
                    finally:
                        self.write_waiting = False

    def shutdown(self, reading=False):
        with self.condition:
            if reading:
                self.read_closed = True
            else:
                self.write_closed = True
            self.condition.notify_all()


class Reader(io.RawIOBase):
    def __init__(self, connection):
        self.connection = connection

    def readable(self):
        return True

    def readinto(self, buffer):
        data = self.connection.incoming.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def close(self):
        if not self.closed:
            super().close()
            self.connection.release_file()


class MemorySocket:
    def __init__(self, incoming, outgoing, before_write=None):
        self.incoming = incoming
        self.outgoing = outgoing
        self.before_write = before_write
        self.lock = threading.Lock()
        self.files = 0
        self.closed = False

    def makefile(self, mode, buffering=-1):
        if mode != "rb":
            raise AssertionError(f"Unexpected stream mode: {mode}")
        with self.lock:
            self.files += 1
        return io.BufferedReader(Reader(self))

    def release_file(self):
        with self.lock:
            self.files -= 1
            if self.closed and self.files == 0:
                self.shutdown(socket.SHUT_RDWR)

    def sendall(self, data):
        if self.before_write:
            self.before_write(bytes(data))
        self.outgoing.write(data)

    def recv(self, count, flags=0):
        return self.incoming.read(count, peek=bool(flags & socket.MSG_PEEK))

    def shutdown(self, how):
        if how in (socket.SHUT_RD, socket.SHUT_RDWR):
            self.incoming.shutdown(reading=True)
        if how in (socket.SHUT_WR, socket.SHUT_RDWR):
            self.outgoing.shutdown()

    def close(self):
        with self.lock:
            self.closed = True
            if self.files == 0:
                self.shutdown(socket.SHUT_RDWR)

    def readable(self):
        with self.incoming.condition:
            return (
                bool(self.incoming.data)
                or self.incoming.write_closed
                or self.incoming.read_closed
            )

    def waiting_for_body(self):
        with self.incoming.condition:
            return self.incoming.waiting and not self.incoming.data

    def waiting_for_output(self):
        with self.outgoing.condition:
            return self.outgoing.write_waiting


def memory_select(readable, writable, errors, timeout=None):
    if readable and all(isinstance(value, MemorySocket) for value in readable):
        return [value for value in readable if value.readable()], [], []
    return _select(readable, writable, errors, timeout)


class Client(http.client.HTTPConnection):
    def __init__(self, fixture, before_write=None):
        super().__init__("memory", timeout=3)
        self.fixture = fixture
        self.before_write = before_write

    def connect(self):
        request, response = Pipe(), Pipe()
        self.sock = MemorySocket(response, request)
        self.server_socket = MemorySocket(request, response, self.before_write)
        self.fixture.attach(self.sock, self.server_socket)


class HttpFixture:
    def __init__(self, agent, token=None, **options):
        self.application = _Application(agent, token, **options)
        self.clients = []
        self.sockets = []
        self.threads = []
        self.errors = []
        self.select_patch = patch("tinyagent_http.select.select", memory_select)
        self.select_patch.start()

    def attach(self, client, server):
        self.sockets.extend((client, server))
        with self.application._lock:
            self.application._connections.add(server)

        def handle():
            try:
                _Handler(server, ("memory", 0), SimpleNamespace(owner=self.application))
            except (ConnectionError, TimeoutError):
                pass
            except BaseException as error:
                self.errors.append(error)
            finally:
                with self.application._lock:
                    self.application._connections.discard(server)
                server.close()

        thread = threading.Thread(target=handle, daemon=True)
        self.threads.append(thread)
        thread.start()

    def headers(self, method, path, headers=(), before_write=None):
        client = Client(self, before_write)
        self.clients.append(client)
        client.putrequest(method, path)
        for name, value in headers:
            client.putheader(name, value)
        client.endheaders()
        return client

    def call(self, method, path, body=None, headers=(), before_write=None):
        data = json.dumps(body).encode() if body is not None else b""
        headers = [*headers, ("Content-Length", str(len(data)))]
        if body is not None:
            headers.append(("Content-Type", "application/json"))
        client = self.headers(method, path, headers, before_write)
        if data:
            client.send(data)
        return client.getresponse()

    def session(self, headers=()):
        response = self.call("POST", "/sessions", headers=headers)
        if response.status != 201:
            raise AssertionError(f"Session creation failed: {response.status}")
        return json.loads(response.read())["sessionId"]

    def prompt(self, session, message, before_write=None):
        return self.call(
            "POST",
            f"/sessions/{session}/prompts",
            {"message": message},
            before_write=before_write,
        )

    def wait_idle(self, session):
        with self.application._lock:
            run = self.application._sessions[session].active
        if run and not run.done.wait(2):
            raise AssertionError("HTTP run did not finish")

    def close(self):
        for connection in self.sockets:
            connection.shutdown(socket.SHUT_RDWR)
        self.application.close()
        for thread in self.threads:
            thread.join(3)
        for client in self.clients:
            client.close()
        self.select_patch.stop()
        if any(thread.is_alive() for thread in self.threads):
            raise AssertionError("HTTP handler did not finish")
        if self.errors:
            raise self.errors[0]


def until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("HTTP condition did not become true")
        time.sleep(0.005)
