"""Session storage: the Storage interface, FileStorage, MemoryStorage, and its test suite.

A Session writes every step through four operations. FileStorage keeps one
append-only JSONL journal per session; a failed write poisons the open handle.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from copy import deepcopy
from pathlib import Path
from typing import Protocol

from tinyagent_types import JsonObject

SESSION_ID = re.compile(r"s_[a-zA-Z0-9_-]{1,100}")


class SessionBusyError(RuntimeError):
    """The session has another writer or an active operation."""


class SessionCorruptionError(RuntimeError):
    """A complete journal record is invalid. The file has not been repaired."""


class SessionFencedError(RuntimeError):
    """Another writer appended to this session first. Nothing was written."""

    code = "session_fenced"


class StorageError(OSError):
    """An append failed for a reason other than fencing. Its outcome is unknown."""

    code = "storage_error"


class SessionFailedError(RuntimeError):
    """This Session object saw a storage failure and takes no more work. Reopen the session."""

    code = "session_failed"


class Storage(Protocol):
    """Where a Session keeps its records. Implement these four to add a store.

    A record is a JSON object; position in the session is its sequence, from 1.
    """

    def load(self, session_id: str) -> list[JsonObject] | None:
        """Every record in order, or None when the session does not exist."""

    def append(self, session_id: str, expected: int, record: JsonObject) -> int:
        """Durably add one record and return its sequence, expected + 1.

        Raise SessionFencedError, writing nothing, unless exactly `expected`
        records are already stored. Any other exception means the outcome is
        unknown, and the Session stops until it is reopened.
        """

    def list(self) -> list[str]:
        """The stored session IDs."""

    def release(self, session_id: str) -> None:
        """The session was closed. Free handles and locks; never delete data."""


class MemoryStorage:
    """Records held in this process. Share one instance to reopen sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, list[JsonObject]] = {}
        self._lock = threading.Lock()

    def load(self, session_id: str) -> list[JsonObject] | None:
        with self._lock:
            records = self._sessions.get(session_id)
            return None if records is None else deepcopy(records)

    def append(self, session_id: str, expected: int, record: JsonObject) -> int:
        with self._lock:
            if expected != len(self._sessions.get(session_id, ())):
                raise SessionFencedError("another writer appended to this session")
            self._sessions.setdefault(session_id, []).append(deepcopy(record))
            return expected + 1

    def list(self) -> list[str]:
        with self._lock:
            return sorted(self._sessions)

    def release(self, session_id: str) -> None:
        pass


class FileStorage:
    """One JSONL journal per session in a directory, each line with a SHA-256.

    The first load or append of a session takes its lock file, so a second
    FileStorage (in this process or another) fails fast with SessionBusyError
    until release. Nothing is touched before the first operation.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self._journals: dict[str, Journal] = {}
        self._lock = threading.Lock()

    def _path(self, session_id: str) -> Path:
        if not SESSION_ID.fullmatch(session_id):
            raise ValueError("invalid session ID")
        return self.directory / f"{session_id}.jsonl"

    def load(self, session_id: str) -> list[JsonObject] | None:
        with self._lock:
            journal = self._journals.get(session_id)
            if journal is not None:
                return journal.read()
            try:
                journal = Journal(self._path(session_id), create=False)
            except FileNotFoundError:
                return None
            self._journals[session_id] = journal
            records, journal.records = journal.records, []
            return records

    def append(self, session_id: str, expected: int, record: JsonObject) -> int:
        with self._lock:
            journal = self._journals.get(session_id)
            if journal is None:
                path = self._path(session_id)
                try:
                    if expected == 0:
                        self.directory.mkdir(parents=True, exist_ok=True)
                    journal = Journal(path, create=expected == 0)
                except (FileExistsError, FileNotFoundError) as error:
                    raise SessionFencedError("another writer appended to this session") from error
                journal.records = []
                self._journals[session_id] = journal
            if expected != journal.count:
                raise SessionFencedError("another writer appended to this session")
            journal.append(record)
            return journal.count

    def list(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        return sorted(path.stem for path in self.directory.glob("s_*.jsonl")
                      if SESSION_ID.fullmatch(path.stem))

    def release(self, session_id: str) -> None:
        with self._lock:
            journal = self._journals.pop(session_id, None)
        if journal:
            journal.close()


def check_storage(storage: Storage, session_id: str = "s_storage_check") -> None:
    """The storage contract as assertions. Run it against a new, empty store."""

    def record(sequence: int, **data: object) -> JsonObject:
        return {"type": "item", "version": 1, "sequence": sequence, **data}

    def fenced(expected: int) -> bool:
        try:
            storage.append(session_id, expected, record(expected + 1, lost=True))
        except SessionFencedError:
            return True
        return False

    assert storage.load(session_id) is None, "an unknown session must load as None"
    assert session_id not in storage.list(), "a new store must not list the session"
    assert fenced(1), "append to a missing session with expected > 0 must be fenced"
    assert storage.load(session_id) is None, "a fenced append must write nothing"
    first = {"type": "session", "version": 1, "sequence": 1, "sessionId": session_id}
    assert storage.append(session_id, 0, first) == 1, "create must return sequence 1"
    assert fenced(0), "a duplicate create must be fenced"
    assert storage.append(session_id, 1, record(2, text="caf\u00e9 \n \u2603")) == 2
    assert storage.append(session_id, 2, record(3, nested={"list": [1, 2.5, None, True]})) == 3
    assert fenced(2) and fenced(4), "append needs expected to equal the stored count"
    expected = [first, record(2, text="caf\u00e9 \n \u2603"),
                record(3, nested={"list": [1, 2.5, None, True]})]
    assert storage.load(session_id) == expected, "load must return the appended records in order"
    assert session_id in storage.list(), "list must include the session"
    storage.release(session_id)
    assert storage.load(session_id) == expected, "release must keep the data"
    assert storage.append(session_id, 3, record(4)) == 4, "append must work after release"
    assert len(storage.load(session_id)) == 4, "sequences must be gapless"
    storage.release(session_id)


class Journal:
    """One open journal file and its lock. `records` is what was read at open."""

    def __init__(self, path: Path, *, create: bool) -> None:
        self._file = None
        self.failed = False
        self.records: list[JsonObject] = []
        self.count = 0
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
            self.records = self.read()
            if create and os.name == "posix":
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except BaseException:
            self.close()
            raise

    def read(self) -> list[JsonObject]:
        """Read and check every record, dropping a torn final line."""
        self._file.seek(0)
        data = self._file.read()
        end = data.rfind(b"\n") + 1
        records: list[JsonObject] = []
        for line in data[:end].splitlines():
            try:
                envelope = json.loads(line)
                payload = envelope["payload"]
                record = json.loads(payload)
                if (envelope["sha256"] != hashlib.sha256(payload.encode()).hexdigest()
                        or not isinstance(record, dict) or record.get("version") != 1
                        or record.get("sequence") != len(records) + 1
                        or not isinstance(record.get("type"), str)):
                    raise ValueError("invalid record")
                records.append(record)
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise SessionCorruptionError(f"invalid session record {len(records) + 1}") from error
        if end != len(data):
            self._file.truncate(end)
            os.fsync(self._file.fileno())
        self._file.seek(0, os.SEEK_END)
        self.count = len(records)
        return records

    def append(self, value: JsonObject) -> JsonObject:
        if self.failed or self._file is None:
            raise RuntimeError("journal is closed or failed; close and reopen the session")
        record = {**value, "version": 1, "sequence": self.count + 1}
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
        self.count += 1
        return json.loads(payload)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        self._lock.close()
