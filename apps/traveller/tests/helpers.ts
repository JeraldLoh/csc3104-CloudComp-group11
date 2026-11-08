import { openCareRouteDB } from "../src/lib/db";
import { Outbox, type CheckinDraft } from "../src/lib/outbox";
import type { SendResult, Transport } from "../src/lib/api";
import type { Checkin, CheckinBatch } from "../src/types";

let n = 0;
export const freshDb = () => openCareRouteDB(`test-${Date.now()}-${n++}`);

export const T0 = Date.parse("2026-10-16T08:00:00Z");

export function draft(over: Partial<CheckinDraft> = {}): CheckinDraft {
  return {
    tripId: "T-001", travellerId: "U-042", timestamp: "2026-10-16T08:00:00Z",
    lat: 1.4052, lng: 103.9024, accuracyM: 12, speedMps: 1.3, headingDeg: 85,
    stepIndex: 2, routeVersion: 3, batteryPct: 64, status: "active", ...over,
  };
}

export async function makeOutbox(now = () => T0) {
  const db = await freshDb();
  return { db, outbox: new Outbox(db, now) };
}

type Call =
  | { kind: "single"; checkin: Checkin; keepalive: boolean }
  | { kind: "batch"; batch: CheckinBatch }
  | { kind: "post"; path: string; body: unknown };

/** Records every request and answers from a script (default: everything accepted). */
export class FakeTransport implements Transport {
  calls: Call[] = [];
  inFlight = 0;
  maxInFlight = 0;
  respond: (call: Call) => SendResult | Promise<SendResult> = (call) =>
    call.kind === "batch"
      ? { kind: "ok", body: { accepted: call.batch.checkins.length, duplicates: 0 } }
      : { kind: "ok", body: { accepted: 1, duplicates: 0 } };

  private async run(call: Call) {
    this.calls.push(call);
    this.inFlight++;
    this.maxInFlight = Math.max(this.maxInFlight, this.inFlight);
    try {
      await new Promise((r) => setTimeout(r, 1)); // a real request takes time
      return await this.respond(call);
    } finally {
      this.inFlight--;
    }
  }

  postCheckin(checkin: Checkin, opts: { keepalive?: boolean } = {}) {
    return this.run({ kind: "single", checkin, keepalive: !!opts.keepalive });
  }
  postBatch(batch: CheckinBatch) {
    return this.run({ kind: "batch", batch });
  }
  post(path: string, body: unknown) {
    return this.run({ kind: "post", path, body });
  }

  /** Every seq the server has been sent, in order. */
  seqs(): number[] {
    return this.calls.flatMap((c) =>
      c.kind === "single" ? [c.checkin.seq] : c.kind === "batch" ? c.batch.checkins.map((x) => x.seq) : [],
    );
  }
}

/** Timers we fire by hand, so backoff can be tested without waiting. */
export class ManualTimers {
  pending: { fn: () => void; ms: number }[] = [];
  set = (fn: () => void, ms: number) => {
    const t = { fn, ms };
    this.pending.push(t);
    return t;
  };
  clear = (t: unknown) => {
    this.pending = this.pending.filter((p) => p !== t);
  };
  fireAll() {
    const due = this.pending;
    this.pending = [];
    due.forEach((t) => t.fn());
  }
}

export const noLock = (fn: () => Promise<void>) => fn();
