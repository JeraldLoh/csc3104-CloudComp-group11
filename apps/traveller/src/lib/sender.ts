// Moves check-ins from the outbox to the Check-in Service.
//
// Rules, and why:
//  * One flush at a time (single-flight). Two flushes running together could send
//    seq 12 before seq 11, or send the same check-in twice in parallel.
//  * One flush across tabs too, using the Web Locks API where the browser has it.
//  * Oldest first. Up to date: a single check-in goes to /api/checkins. Behind
//    (several waiting, or any marked buffered): they go together to
//    /api/checkins/batch, which is one request instead of hundreds after a tunnel.
//    A busy server may answer for only the oldest part of a batch ("processed"); the
//    rest go in the next request, so every request finishes quickly and makes progress.
//  * Failed sends retry with exponential backoff plus "full jitter": wait a random
//    time between 0 and min(cap, base * 2^n). The randomness stops hundreds of phones
//    that lost signal at the same moment (a train in a tunnel) from all retrying at
//    the same instant and knocking the service over again (a "thundering herd").
//  * The backoff is a real "not before" time: while it runs, new check-ins wait in the
//    outbox instead of each triggering its own request (otherwise the 10 s heartbeat
//    would cap every backoff at 10 s, and a struggling server would get no relief).
//  * If the server says how long to wait (503 + Retry-After, when it's shedding load),
//    the phone waits at least that long, plus a random extra of up to the same again,
//    so the phones it turned away don't all come back in the same second.
//  * Coming back online retries after a short random delay (0-2 s): many phones regain
//    signal at the same moment (a cell tower or train coming back), so this spreads
//    their catch-up batches. Returning to the app retries straight away.
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

/**
 * How long to wait before the next attempt: the jittered exponential backoff, but never
 * less than the server's Retry-After, which itself gets up to 100% random extra.
 */
export function retryDelay(
  failures: number, retryAfterMs: number | undefined, random: () => number, baseMs: number, capMs: number,
): number {
  const backoff = backoffDelay(failures, random, baseMs, capMs);
  if (!retryAfterMs) return backoff;
  return Math.max(backoff, Math.floor(retryAfterMs * (1 + random())));
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

  /** Ask for a flush. Safe to call as often as you like. Waits out a running backoff. */
  flush(opts: { keepalive?: boolean } = {}): Promise<void> {
    if (opts.keepalive) this.keepaliveNext = true;
    if (this.timer !== null) {
      // Backing off: leave new check-ins in the outbox; the timer sends them all together.
      return this.refreshPending();
    }
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

  /**
   * Back online, or the app came to the front: forget the backoff and try again,
   * either now or after a random delay of up to `jitterMs` (used on reconnect).
   */
  retryNow(opts: { jitterMs?: number } = {}): Promise<void> {
    this.cancelTimer();
    this.state.nextRetryAt = null;
    const wait = opts.jitterMs ? Math.floor(this.d.random() * opts.jitterMs) : 0;
    if (wait <= 0) return this.flush();
    this.schedule(wait);
    this.update({ nextRetryAt: this.d.now() + wait });
    return Promise.resolve();
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
        // A busy server may handle only the oldest part of a batch (its time budget ran
        // out): remove exactly those; the loop sends the rest straight away.
        const done = ack.processed === undefined ? items : items.slice(0, Math.max(0, ack.processed));
        await this.d.outbox.remove(done);
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
        const delay = retryDelay(failures, r.retryAfterMs, this.d.random, this.d.backoffBaseMs, this.d.backoffCapMs);
        this.schedule(delay);
        this.update({ consecutiveFailures: failures, nextRetryAt: this.d.now() + delay, lastError: r.reason });
        return false;
      }
    }
  }

  private schedule(ms: number) {
    this.cancelTimer();
    this.timer = this.d.setTimer(() => {
      this.timer = null;
      void this.flush();
    }, ms);
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
