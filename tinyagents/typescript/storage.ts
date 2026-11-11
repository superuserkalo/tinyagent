import { isItem, randomId, type Item } from "./types.ts";

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

/** A rule of the storage contract that a store broke. Named as node:assert names its errors. */
class AssertionError extends Error {
  readonly code = "ERR_ASSERTION";

  constructor(message: string, options?: { cause: unknown }) {
    super(message, options);
    this.name = "AssertionError";
  }
}

/** Records compare as JSON values: the same keys in any order, the same items in the same order. */
function sameJson(left: unknown, right: unknown): boolean {
  if (Array.isArray(left) || Array.isArray(right))
    return Array.isArray(left) && Array.isArray(right) && left.length === right.length &&
      left.every((item, index) => sameJson(item, right[index]));
  if (isItem(left) && isItem(right)) {
    const keys = Object.keys(left);
    return keys.length === Object.keys(right).length &&
      keys.every((key) => Object.hasOwn(right, key) && sameJson(left[key], right[key]));
  }
  return Object.is(left, right);
}

/**
 * The storage contract as a check any implementation can run. It throws an
 * AssertionError at the first rule the store breaks, and leaves two small
 * sessions behind, because a store never deletes.
 */
export async function checkStorage(storage: Storage): Promise<void> {
  const unique = randomId();
  const [first, second] = [`s_check_${unique}_a`, `s_check_${unique}_b`];
  const header = { type: "session", note: "created by checkStorage" };
  const check = (holds: boolean, rule: string) => { if (!holds) throw new AssertionError(rule); };
  const fenced = async (expected: number, rule: string) => {
    try {
      await storage.append(first, expected, { type: "stray" });
    } catch (error) {
      if (error instanceof FencedError) return;
      throw new AssertionError(rule, { cause: error });
    }
    throw new AssertionError(`Missing expected rejection (FencedError): ${rule}`);
  };

  check(await storage.load(first) === undefined, "an unknown session is not found");
  await fenced(1, "appending to a session that does not exist is fenced");
  check(await storage.append(first, 0, header) === 1, "the first record has sequence 1");
  await fenced(0, "creating a session twice is fenced");
  const record = { type: "item", item: { role: "user", content: "naïve ☃ \n line", nested: [1, null, { deep: true }] } };
  check(await storage.append(first, 1, record) === 2, "append returns expected + 1");
  await fenced(1, "an append behind the stored count is fenced");
  await fenced(3, "an append ahead of the stored count is fenced");
  check(await storage.append(first, 2, { type: "end" }) === 3, "a fenced append wrote nothing");
  const stored = [{ ...header, sequence: 1 }, { ...record, sequence: 2 }, { type: "end", sequence: 3 }];
  check(sameJson(await storage.load(first), stored), "load returns every record, in order, with gapless sequences");

  check(await storage.append(second, 0, header) === 1, "sessions are independent");
  const listed = await storage.list();
  check(listed.includes(first) && listed.includes(second), "list names every stored session");

  await storage.release(first);
  check(sameJson(await storage.load(first), stored), "release keeps the data");
  check(await storage.append(first, 3, { type: "later" }) === 4, "a released session can be appended to again");
  await storage.release(first);
  await storage.release(second);
  check((await storage.load(first))?.length === 4, "a session holds every record appended to it");
}
