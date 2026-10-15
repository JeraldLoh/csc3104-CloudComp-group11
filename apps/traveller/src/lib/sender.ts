// Moves check-ins from the outbox to the Check-in Service.
//
// Rules, and why:
//  * One flush at a time (single-flight). Two flushes running together could send
//    seq 12 before seq 11, or send the same check-in twice in parallel.
//  * One flush across tabs too, using the Web Locks API where the browser has it.
//  * Oldest first. Up to date: a single check-in goes to /api/checkins. Behind
//    (several waiting, or any marked buffered): they go together to
//    /api/checkins/batch, which is one request instead of hundreds after a tunnel.
//  * Failed sends retry with exponential backoff plus "full jitter": wait a random
//    time between 0 and min(cap, base * 2^n). The randomness stops hundreds of phones
//    that lost signal at the same moment (a train in a tunnel) from all retrying at
//    the same instant and knocking the service over again (a "thundering herd").
//  * Coming back online, or the app returning to the foreground, retries immediately.
import { asAck, type SendResult, type Transport } from "./api";
import type { Outbox } from "./outbox";
import type { OutboxItem } from "./db";
import type { CheckinBatch } from "../types";

export interface SenderState {
  pending: number;
  sending: boolean;
  consecutiveFailures: number;
  nextRetryAt: number | null;
  lastSuccessAt: number | null;
  lastError: string | null;
  sent: number;        // accepted by the server (this session)
  duplicates: number;  // server already had them (this session)
  rejected: number;    // server said invalid; dropped
  authRequired: boolean;
}

export interface SenderDeps {
  outbox: Outbox;
  transport: Transport;
  isOnline?: () => boolean;
  now?: () => number;
  random?: () => number;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (t: unknown) => void;
  /** Runs fn holding a cross-tab lock, or skips if another tab holds it. */
  withLock?: (fn: () => Promise<void>) => Promise<void>;
  maxBatch?: number;
  backoffBaseMs?: number;
  backoffCapMs?: number;
}

export const DEFAULTS = { maxBatch: 100, backoffBaseMs: 1000, backoffCapMs: 60_000 };

export function backoffDelay(failures: number, random: () => number, baseMs: number, capMs: number): number {
  const ceiling = Math.min(capMs, baseMs * 2 ** Math.max(0, failures - 1));
  return Math.max(250, Math.floor(random() * ceiling));
}

/** Web Locks: only one tab flushes at a time. Falls back to no lock if unsupported. */
export function webLock(name: string) {
  return async (fn: () => Promise<void>) => {
    const locks = (globalThis.navigator as Navigator | undefined)?.locks;
    if (!locks) return fn();
    await locks.request(name, { ifAvailable: true }, async (lock) => {
      if (lock) await fn();
    });
  };
}

export class Sender {
  private running: Promise<void> | null = null;
  private rerun = false;
  private timer: unknown = null;
  private listeners = new Set<(s: SenderState) => void>();
  private keepaliveNext = false;

  state: SenderState = {
    pending: 0, sending: false, consecutiveFailures: 0, nextRetryAt: null, lastSuccessAt: null,
    lastError: null, sent: 0, duplicates: 0, rejected: 0, authRequired: false,
  };

  private d: Required<SenderDeps>;

  constructor(deps: SenderDeps) {
    this.d = {
      isOnline: () => (typeof navigator === "undefined" ? true : navigator.onLine),
      now: Date.now,
      random: Math.random,
      setTimer: (fn, ms) => setTimeout(fn, ms),
      clearTimer: (t) => clearTimeout(t as ReturnType<typeof setTimeout>),
      withLock: webLock("careroute-outbox"),
      ...DEFAULTS,
      ...deps,
    } as Required<SenderDeps>;
  }

  subscribe(fn: (s: SenderState) => void): () => void {
    this.listeners.add(fn);
    fn(this.state);
    return () => this.listeners.delete(fn);
  }

  /** Ask for a flush. Safe to call as often as you like. */
  flush(opts: { keepalive?: boolean } = {}): Promise<void> {
    if (opts.keepalive) this.keepaliveNext = true;
    if (this.running) {
      this.rerun = true; // something new arrived mid-flush; go round again afterwards
      return this.running;
    }
    this.running = (async () => {
      try {
        do {
          this.rerun = false;
          await this.d.withLock(() => this.drain());
        } while (this.rerun);
      } finally {
        this.running = null;
        await this.refreshPending();
      }
    })();
    return this.running;
  }

  /** Back online, or the app came to the front: forget the backoff and try now. */
  retryNow(): Promise<void> {
    this.cancelTimer();
    this.state.nextRetryAt = null;
    return this.flush();
  }

  /** After the traveller logs in again. */
  resumeAfterLogin(): Promise<void> {
    this.update({ authRequired: false });
    return this.retryNow();
  }

  private async drain(): Promise<void> {
    if (this.state.authRequired) return;
    this.update({ sending: true });
    try {
      for (;;) {
        const items = await this.d.outbox.peek(this.d.maxBatch);
        if (items.length === 0) {
          this.update({ consecutiveFailures: 0, nextRetryAt: null });
          return;
        }
        if (!this.d.isOnline()) {
          // No point trying. Mark them late; the 'online' event will call retryNow().
          await this.d.outbox.markBuffered(items.filter((i) => !i.buffered), false);
          this.update({ lastError: "offline" });
          return;
        }
        const result = await this.send(items);
        if (!(await this.handle(result, items))) return;
      }
    } finally {
      this.update({ sending: false });
      await this.refreshPending();
    }
  }

  private send(items: OutboxItem[]): Promise<SendResult> {
    const live = items.length === 1 && !items[0].buffered;
    if (live) {
      const keepalive = this.keepaliveNext;
      this.keepaliveNext = false;
      return this.d.transport.postCheckin(items[0].checkin, { keepalive });
    }
    const { tripId, travellerId } = items[0].checkin;
    const batch: CheckinBatch = {
      tripId,
      travellerId,
      checkins: items.map(({ checkin: { tripId: _t, travellerId: _u, ...rest } }) => rest),
    };
    return this.d.transport.postBatch(batch);
  }

  /** Returns true to keep draining, false to stop for now. */
  private async handle(r: SendResult, items: OutboxItem[]): Promise<boolean> {
    switch (r.kind) {
      case "ok": {
        const ack = asAck(r.body);
        await this.d.outbox.remove(items);
        this.update({
          sent: this.state.sent + ack.accepted,
          duplicates: this.state.duplicates + ack.duplicates,
          consecutiveFailures: 0, nextRetryAt: null, lastSuccessAt: this.d.now(), lastError: null,
        });
        return true;
      }
      case "invalid": {
        if (items.length > 1) {
          // One bad check-in shouldn't block the rest. Send them one by one to find it.
          for (const item of items) {
            const single = await this.d.transport.postCheckin(item.checkin);
            if (!(await this.handle(single, [item]))) return false;
          }
          return true;
        }
        // Retrying a request the server calls invalid would block the outbox forever.
        console.warn("CareRoute: check-in rejected and dropped", r.status, r.message, items[0].checkin);
        await this.d.outbox.remove(items);
        this.update({ rejected: this.state.rejected + 1, lastError: `rejected: ${r.message}` });
        return true;
      }
      case "unauthorized":
        this.update({ authRequired: true, lastError: "please log in again" });
        return false;
      case "retry": {
        await this.d.outbox.markBuffered(items, true);
        const failures = this.state.consecutiveFailures + 1;
        const delay = backoffDelay(failures, this.d.random, this.d.backoffBaseMs, this.d.backoffCapMs);
        this.cancelTimer();
        this.timer = this.d.setTimer(() => {
          this.timer = null;
          void this.flush();
        }, delay);
        this.update({ consecutiveFailures: failures, nextRetryAt: this.d.now() + delay, lastError: r.reason });
        return false;
      }
    }
  }

  private cancelTimer() {
    if (this.timer !== null) this.d.clearTimer(this.timer);
    this.timer = null;
  }

  private async refreshPending() {
    this.update({ pending: await this.d.outbox.count() });
  }

  private update(patch: Partial<SenderState>) {
    this.state = { ...this.state, ...patch };
    for (const fn of this.listeners) fn(this.state);
  }

  dispose() {
    this.cancelTimer();
    this.listeners.clear();
  }
}
