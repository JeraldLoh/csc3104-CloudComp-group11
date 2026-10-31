// The outbox: every check-in is written here BEFORE anyone tries to send it.
// Sending then removes it only after the server answers 202. So a check-in survives
// no signal, a crashed tab, a dead server or a phone restart: it waits here until it
// gets through. This is the "transactional outbox" pattern, on the phone.
import { metaKeys, type DB, type OutboxItem } from "./db";
import type { Checkin } from "../types";

export type CheckinDraft = Omit<Checkin, "seq" | "bufferedOffline">;

/** Oldest check-ins are dropped past this, so storage can't grow without limit
 *  (about 2.5 days of check-ins at one every 10 s). */
export const OUTBOX_CAP = 20_000;

export class Outbox {
  /** Check-ins dropped because the outbox was full. Shown in the debug panel. */
  dropped = 0;

  constructor(
    private db: DB,
    private now: () => number = Date.now,
    private cap: number = OUTBOX_CAP,
  ) {}

  /**
   * Give the check-in its sequence number and store it, in ONE transaction.
   *
   * seq must never repeat for a trip: the server treats a repeated tripId + seq as a
   * retry and ignores it. Doing "next seq" and "store check-in" atomically means two
   * tabs, or a crash between the two steps, can't produce a repeat or a gap.
   *
   * The first seq for a trip on this phone is the current time in seconds, not 0.
   * If the app's storage is wiped mid-trip (or the traveller switches phones), the
   * counter restarts above every seq already sent, instead of at 0, where every new
   * check-in would look like an old duplicate and be silently dropped.
   */
  async enqueue(draft: CheckinDraft, opts: { offline: boolean }): Promise<OutboxItem> {
    const tx = this.db.transaction(["meta", "outbox"], "readwrite");
    const meta = tx.objectStore("meta");
    const key = metaKeys.seq(draft.tripId);
    const last = (await meta.get(key)) as number | undefined;
    const seq = last === undefined ? Math.floor(this.now() / 1000) : last + 1;
    const item: OutboxItem = {
      tripId: draft.tripId,
      seq,
      checkin: { ...draft, seq, bufferedOffline: opts.offline },
      createdAt: this.now(),
      attempts: 0,
      buffered: opts.offline,
    };
    await meta.put(seq, key);
    await tx.objectStore("outbox").put(item);
    await tx.done;
    await this.enforceCap();
    return item;
  }

  /** Oldest check-ins first, all from the same trip (a batch is for one trip). */
  async peek(limit: number): Promise<OutboxItem[]> {
    const out: OutboxItem[] = [];
    let cursor = await this.db.transaction("outbox").store.openCursor();
    while (cursor && out.length < limit) {
      if (out.length > 0 && cursor.value.tripId !== out[0].tripId) break;
      out.push(cursor.value);
      cursor = await cursor.continue();
    }
    return out;
  }

  async remove(items: OutboxItem[]): Promise<void> {
    const tx = this.db.transaction("outbox", "readwrite");
    await Promise.all(items.map((i) => tx.store.delete([i.tripId, i.seq])));
    await tx.done;
  }

  /** A send failed or we're offline: these are now late, so mark them buffered. */
  async markBuffered(items: OutboxItem[], countAttempt: boolean): Promise<void> {
    const tx = this.db.transaction("outbox", "readwrite");
    for (const i of items) {
      const cur = await tx.store.get([i.tripId, i.seq]);
      if (!cur) continue; // already sent and removed
      cur.buffered = true;
      cur.checkin.bufferedOffline = true;
      if (countAttempt) cur.attempts += 1;
      await tx.store.put(cur);
    }
    await tx.done;
  }

  count(): Promise<number> {
    return this.db.count("outbox");
  }

  private async enforceCap(): Promise<void> {
    let extra = (await this.count()) - this.cap;
    if (extra <= 0) return;
    const tx = this.db.transaction("outbox", "readwrite");
    let cursor = await tx.store.openCursor();
    while (cursor && extra > 0) {
      await cursor.delete();
      this.dropped += 1;
      extra -= 1;
      cursor = await cursor.continue();
    }
    await tx.done;
  }
}
