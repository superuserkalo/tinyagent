import { createHash } from "node:crypto";
import { openSync, closeSync, readFileSync, statSync, writeSync, ftruncateSync, fsyncSync } from "node:fs";
import { dirname } from "node:path";
import type { DatabaseSync } from "node:sqlite";
import { isItem, type Item } from "./types.ts";

export class SessionBusyError extends Error {}
export class SessionCorruptionError extends Error {}

/** The lock is native and released by the OS on process exit, including crashes. */
export class Journal {
  private file: number | undefined;
  private readonly lock: DatabaseSync;
  readonly records: Item[] = [];
  failed = false;

  constructor(path: string, create: boolean) {
    // Checked first so a mistyped ID fails without leaving a lock file behind.
    if (!create) statSync(path);
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
      const data = readFileSync(this.file);
      const end = data.lastIndexOf(0x0a) + 1;
      const decoder = new TextDecoder("utf-8", { fatal: true });
      let start = 0;
      while (start < end) {
        const newline = data.indexOf(0x0a, start);
        try {
          const envelope: unknown = JSON.parse(decoder.decode(data.subarray(start, newline)));
          if (!isItem(envelope) || typeof envelope.payload !== "string" ||
              envelope.sha256 !== createHash("sha256").update(envelope.payload).digest("hex"))
            throw new Error("invalid checksum");
          const record: unknown = JSON.parse(envelope.payload);
          if (!isItem(record) || record.version !== 1 || record.sequence !== this.records.length + 1 ||
              typeof record.type !== "string") throw new Error("invalid record");
          this.records.push(record);
        } catch (error) {
          throw new SessionCorruptionError(`invalid session record ${this.records.length + 1}`, { cause: error });
        }
        start = newline + 1;
      }
      if (end !== data.length) {
        ftruncateSync(this.file, end);
        fsyncSync(this.file);
      }
      this.offset = end;
      if (create && process.platform !== "win32") {
        const directory = openSync(dirname(path), "r");
        try { fsyncSync(directory); } finally { closeSync(directory); }
      }
    } catch (error) {
      this.close();
      throw error;
    }
  }

  private offset = 0;

  append(value: Item): Item {
    if (this.failed || this.file === undefined) throw new Error("journal is closed or failed; reopen the session");
    const payload = JSON.stringify({ ...value, version: 1, sequence: this.records.length + 1 });
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
    } catch (error) {
      this.failed = true;
      throw error;
    }
    const record: unknown = JSON.parse(payload);
    if (!isItem(record)) throw new Error("invalid journal record");
    this.records.push(record);
    return record;
  }

  close(): void {
    if (this.file !== undefined) {
      closeSync(this.file);
      this.file = undefined;
    }
    if (this.lock.isOpen) this.lock.close();
  }
}
