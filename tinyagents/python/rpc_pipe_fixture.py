"""Drive run_rpc in a thread over a real pipe for input and a collector for output."""
import json, os, threading, time
from tinyagent import run_rpc


class Collector:
    def __init__(self):
        self.data = b""
        self.lock = threading.Lock()

    def write(self, chunk):
        with self.lock:
            self.data += bytes(chunk)
        return len(chunk)

    def flush(self):
        pass

    def lines(self):
        with self.lock:
            return [json.loads(line) for line in self.data.split(b"\n") if line]


class Rpc:
    def __init__(self, agent, **options):
        read_fd, self._write_fd = os.pipe()
        self._source = os.fdopen(read_fd, "rb", buffering=0)
        self.out = Collector()
        self.failure = None

        def work():
            try:
                run_rpc(agent, self._source, self.out, **options)
            except BaseException as error:
                self.failure = error

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()

    def send(self, command):
        os.write(self._write_fd, json.dumps(command).encode() + b"\n")

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in self.out.lines():
                if predicate(line):
                    return line
            if not self.thread.is_alive():
                break
            time.sleep(0.02)
        for line in self.out.lines():
            if predicate(line):
                return line
        return None

    def close(self):
        try:
            os.close(self._write_fd)
        except OSError:
            pass
        self.thread.join(5)
