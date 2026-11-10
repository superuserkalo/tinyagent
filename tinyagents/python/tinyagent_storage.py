"""Append-only local session journal. A failed write poisons the open handle."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from tinyagent_types import JsonObject


class SessionBusyError(RuntimeError):
    """The session has another writer or an active operation."""


class SessionCorruptionError(RuntimeError):
    """A complete journal record is invalid. The file has not been repaired."""


class Journal:
    def __init__(self, path: Path, *, create: bool) -> None:
        self._file = None
        self.failed = False
        self.records: list[JsonObject] = []
        if not create and not path.is_file():
            # Checked first so a mistyped ID does not leave a lock file behind.
            raise FileNotFoundError(f"no saved session at {path}")
        self._lock = sqlite3.connect(str(path) + ".lock", timeout=0, check_same_thread=False)
        try:
            # SQLite supplies native cross-process locks on Unix and Windows.
            # No PID files or stale-lock deletion, including after a crash.
            self._lock.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as error:
            self._lock.close()
            raise SessionBusyError("session is open in another writer") from error
        try:
            descriptor = os.open(path, os.O_RDWR | (os.O_CREAT | os.O_EXCL if create else 0), 0o600)
            self._file = os.fdopen(descriptor, "r+b", buffering=0)
            data = self._file.read()
            end = data.rfind(b"\n") + 1
            for line in data[:end].splitlines():
                try:
                    envelope = json.loads(line)
                    payload = envelope["payload"]
                    record = json.loads(payload)
                    if (envelope["sha256"] != hashlib.sha256(payload.encode()).hexdigest()
                            or not isinstance(record, dict) or record.get("version") != 1
                            or record.get("sequence") != len(self.records) + 1
                            or not isinstance(record.get("type"), str)):
                        raise ValueError("invalid record")
                    self.records.append(record)
                except (ValueError, TypeError, KeyError, AttributeError) as error:
                    raise SessionCorruptionError(f"invalid session record {len(self.records) + 1}") from error
            if end != len(data):
                self._file.truncate(end)
                os.fsync(descriptor)
            self._file.seek(0, os.SEEK_END)
            if create and os.name == "posix":
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except BaseException:
            self.close()
            raise

    def append(self, value: JsonObject) -> JsonObject:
        if self.failed or self._file is None:
            raise RuntimeError("journal is closed or failed; close and reopen the session")
        record = {**value, "version": 1, "sequence": len(self.records) + 1}
        payload = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        encoded = (json.dumps({"payload": payload, "sha256": hashlib.sha256(payload.encode()).hexdigest()},
                              ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        try:
            remaining = memoryview(encoded)
            while remaining:
                count = self._file.write(remaining)
                if not count:
                    raise OSError("session write made no progress")
                remaining = remaining[count:]
            os.fsync(self._file.fileno())
        except BaseException:
            self.failed = True
            raise
        saved = json.loads(payload)
        self.records.append(saved)
        return saved

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        self._lock.close()
