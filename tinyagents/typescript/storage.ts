import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { openSync, closeSync, readSync, fstatSync, statSync, writeSync, ftruncateSync, fsyncSync, mkdirSync,
  readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import type { DatabaseSync } from "node:sqlite";
import { isItem, type Item } from "./types.ts";

export class SessionBusyError extends Error {
  readonly code = "session_busy";
}
export class SessionCorruptionError extends Error {}

/** Another writer got there first: the append wrote nothing. */
export class FencedError extends Error {
  readonly code = "session_fenced";

  constructor(message = "another writer has appended to this session; reopen it") {
    super(message);
    this.name = "FencedError";
  }
}

/**
 * Where sessions are kept. A session is an ordered list of JSON records; the
 * store may serialise them however it likes. Every operation may return its
 * value directly or as a promise.
 */
export interface Storage {
  /** The session's records in order, each with its `sequence` (1..n), or undefined when there is no such session. */
  load(sessionId: string): Item[] | undefined | Promise<Item[] | undefined>;
  /**
   * Store `record` as number `expected + 1` and return that sequence. It must be
   * durable before this returns. When the session does not hold exactly
   * `expected` records, write nothing and throw FencedError. `expected` 0 creates
   * the session. Any other error means the outcome is unknown.
   */
  append(sessionId: string, expected: number, record: Item): number | Promise<number>;
  list(): string[] | Promise<string[]>;
  /** Let go of the session's resources. Never deletes data. */
  release(sessionId: string): void | Promise<void>;
}

/** Sessions that live and die with this object. It can be shared by several Sessions. */
export class MemoryStorage implements Storage {
  private readonly sessions = new Map<string, Item[]>();

  load(sessionId: string): Item[] | undefined {
    const records = this.sessions.get(sessionId);
    return records && structuredClone(records);
  }

  append(sessionId: string, expected: number, record: Item): number {
    const records = this.sessions.get(sessionId) ?? [];
    if (records.length !== expected) throw new FencedError();
    records.push(structuredClone({ ...record, sequence: expected + 1 }));
    this.sessions.set(sessionId, records);
    return expected + 1;
  }

  list(): string[] {
    return [...this.sessions.keys()].sort();
  }

  release(): void {}
}

const SESSION_FILE = /^(s_[a-zA-Z0-9_-]{1,100})\.jsonl$/;

/**
 * One open journal file and its writer lock. The lock is native and released
 * by the operating system when the process exits, including a crash. It makes
 * a second writer fail fast; the fenced append is what keeps the journal correct.
 */
class Journal {
  private file: number | undefined;
  private readonly lock: DatabaseSync;
  private offset = 0;
  private failed = false;
  count = 0;

  constructor(path: string, create: boolean) {
    // Loaded on first durable use: Node 22 prints an experimental warning for
    // node:sqlite, which importing the SDK or an in-memory session should not trigger.
    const { DatabaseSync } = process.getBuiltinModule("node:sqlite");
    this.lock = new DatabaseSync(`${path}.lock`);
    try {
      this.lock.exec("BEGIN EXCLUSIVE");
    } catch (error) {
      this.lock.close();
      throw new SessionBusyError("session is open in another writer", { cause: error });
    }
    try {
      this.file = openSync(path, create ? "wx+" : "r+", 0o600);
      if (create && process.platform !== "win32") {
        const directory = openSync(dirname(path), "r");
        try { fsyncSync(directory); } finally { closeSync(directory); }
      }
    } catch (error) {
      this.close();
      throw isItem(error) && error.code === "EEXIST" ? new FencedError("session already exists") : error;
    }
  }

  /** Every complete record. A torn final record, left by a crash mid-write, is discarded. */
  read(): Item[] {
    if (this.file === undefined) throw new Error("journal is closed");
    const data = Buffer.alloc(fstatSync(this.file).size);
    for (let done = 0; done < data.length;) {
      const count = readSync(this.file, data, done, data.length - done, done);
      if (!count) throw new Error("session read made no progress");
      done += count;
    }
    const end = data.lastIndexOf(0x0a) + 1;
    const decoder = new TextDecoder("utf-8", { fatal: true });
    const records: Item[] = [];
    let start = 0;
    while (start < end) {
      const newline = data.indexOf(0x0a, start);
      try {
        const envelope: unknown = JSON.parse(decoder.decode(data.subarray(start, newline)));
        if (!isItem(envelope) || typeof envelope.payload !== "string" ||
            envelope.sha256 !== createHash("sha256").update(envelope.payload).digest("hex"))
          throw new Error("invalid checksum");
        const record: unknown = JSON.parse(envelope.payload);
        if (!isItem(record) || record.sequence !== records.length + 1) throw new Error("invalid record");
        records.push(record);
      } catch (error) {
        throw new SessionCorruptionError(`invalid session record ${records.length + 1}`, { cause: error });
      }
      start = newline + 1;
    }
    if (end !== data.length) {
      ftruncateSync(this.file, end);
      fsyncSync(this.file);
    }
    this.offset = end;
    this.count = records.length;
    return records;
  }

  write(record: Item): void {
    if (this.failed || this.file === undefined) throw new Error("journal is closed or failed; reopen the session");
    if (fstatSync(this.file).size !== this.offset) throw new FencedError();
    const payload = JSON.stringify(record);
    const encoded = Buffer.from(`${JSON.stringify({ payload, sha256: createHash("sha256").update(payload).digest("hex") })}\n`);
    try {
      let written = 0;
      while (written < encoded.length) {
        const count = writeSync(this.file, encoded, written, encoded.length - written, this.offset + written);
        if (!count) throw new Error("session write made no progress");
        written += count;
      }
      fsyncSync(this.file);
      this.offset += written;
      this.count += 1;
    } catch (error) {
      this.failed = true;
      throw error;
    }
  }

  close(): void {
    if (this.file !== undefined) {
      closeSync(this.file);
      this.file = undefined;
    }
    if (this.lock.isOpen) this.lock.close();
  }
}

/**
 * One append-only JSONL file per session in `directory`, each record with a
 * SHA-256, plus a lock file per session. Constructing it touches nothing.
 */
export class FileStorage implements Storage {
  readonly directory: string;
  private readonly journals = new Map<string, Journal>();

  constructor(directory: string) {
    this.directory = directory;
  }

  private open(sessionId: string, create: boolean): Journal | undefined {
    if (!SESSION_FILE.test(`${sessionId}.jsonl`)) throw new TypeError("invalid session ID");
    const path = join(this.directory, `${sessionId}.jsonl`);
    if (create) mkdirSync(this.directory, { recursive: true });
    else {
      // Checked first so a mistyped ID fails without leaving a lock file behind.
      try { statSync(path); }
      catch (error) { if (isItem(error) && error.code === "ENOENT") return undefined; throw error; }
    }
    const journal = new Journal(path, create);
    this.journals.set(sessionId, journal);
    return journal;
  }

  load(sessionId: string): Item[] | undefined {
    const journal = this.journals.get(sessionId) ?? this.open(sessionId, false);
    try {
      return journal?.read();
    } catch (error) {
      this.release(sessionId);
      throw error;
    }
  }

  append(sessionId: string, expected: number, record: Item): number {
    let journal = this.journals.get(sessionId);
    if (!journal) {
      journal = this.open(sessionId, expected === 0);
      if (!journal) throw new FencedError("session does not exist");
      if (expected) this.load(sessionId);
    }
    if (journal.count !== expected) throw new FencedError();
    journal.write({ ...record, sequence: expected + 1 });
    return expected + 1;
  }

  list(): string[] {
    try {
      return readdirSync(this.directory).flatMap((name) => SESSION_FILE.exec(name)?.[1] ?? []).sort();
    } catch (error) {
      if (isItem(error) && error.code === "ENOENT") return [];
      throw error;
    }
  }

  release(sessionId: string): void {
    this.journals.get(sessionId)?.close();
    this.journals.delete(sessionId);
  }
}

/**
 * The storage contract as a check any implementation can run. It throws an
 * AssertionError at the first rule the store breaks, and leaves two small
 * sessions behind, because a store never deletes.
 */
export async function checkStorage(storage: Storage): Promise<void> {
  const unique = randomUUID().replaceAll("-", "");
  const [first, second] = [`s_check_${unique}_a`, `s_check_${unique}_b`];
  const header = { type: "session", note: "created by checkStorage" };
  const fenced = (expected: number, why: string) =>
    assert.rejects(async () => storage.append(first, expected, { type: "stray" }), FencedError, why);

  assert.equal(await storage.load(first), undefined, "an unknown session is not found");
  await fenced(1, "appending to a session that does not exist is fenced");
  assert.equal(await storage.append(first, 0, header), 1, "the first record has sequence 1");
  await fenced(0, "creating a session twice is fenced");
  const record = { type: "item", item: { role: "user", content: "naïve ☃ \n line", nested: [1, null, { deep: true }] } };
  assert.equal(await storage.append(first, 1, record), 2, "append returns expected + 1");
  await fenced(1, "an append behind the stored count is fenced");
  await fenced(3, "an append ahead of the stored count is fenced");
  assert.equal(await storage.append(first, 2, { type: "end" }), 3, "a fenced append wrote nothing");
  const stored = [{ ...header, sequence: 1 }, { ...record, sequence: 2 }, { type: "end", sequence: 3 }];
  assert.deepEqual(await storage.load(first), stored, "load returns every record, in order, with gapless sequences");

  assert.equal(await storage.append(second, 0, header), 1, "sessions are independent");
  const listed = await storage.list();
  assert.ok(listed.includes(first) && listed.includes(second), "list names every stored session");

  await storage.release(first);
  assert.deepEqual(await storage.load(first), stored, "release keeps the data");
  assert.equal(await storage.append(first, 3, { type: "later" }), 4, "a released session can be appended to again");
  await storage.release(first);
  await storage.release(second);
  assert.equal((await storage.load(first))?.length, 4);
}
