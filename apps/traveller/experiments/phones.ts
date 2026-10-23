// Simulated phones for the evaluation (issue 2.5). Run by evaluation/run.py.
//
// Each "outbox" phone runs the app's real Outbox + Sender + HttpTransport (src/lib), on an
// in-memory IndexedDB, exactly as the browser does. A "naive" phone is the baseline a
// simple app would use: send each check-in once, no outbox, no retry.
//
// The network is simulated per phone:
//   silent outage  the phone thinks it's online but requests hang until they time out
//                  (a tunnel, a dead cell); the sender's backoff has to cope
//   known outage   navigator.onLine is false; check-ins wait, then the 'online' event
//                  triggers the (jittered) reconnect, as in the real Tracker
//
// Usage: npx tsx experiments/phones.ts <config.json> <out.json>
import "fake-indexeddb/auto";
import { readFileSync, writeFileSync } from "node:fs";
import { openCareRouteDB, type DB } from "../src/lib/db";
import { Outbox } from "../src/lib/outbox";
import { Sender } from "../src/lib/sender";
import { HttpTransport, type SendResult, type Transport } from "../src/lib/api";
import type { Checkin, CheckinBatch } from "../src/types";

interface Outage {
  startMs: number;
  endMs: number;
  mode: "silent" | "known";
  fraction?: number; // share of phones affected (default all)
}

interface Config {
  name: string;
  baseUrls: string[];
  phones: number;
  kind: "outbox" | "naive";
  tripPrefix: string;
  heartbeatMs: number;
  durationMs: number; // check-ins are generated for this long
  drainMs: number; // then phones keep running (retrying) for this long
  timeoutMs: number;
  backoffBaseMs: number;
  backoffCapMs: number;
  reconnectJitterMs: number;
  jitter: boolean; // false: every random choice is 1 (no randomness, phones act in lockstep)
  outages: Outage[];
  restartAtMs?: number; // app restart (tab closed and reopened) at this time
  persistent?: boolean; // outbox survives the restart (IndexedDB) or not (memory)
  dieOnFailureFraction?: number; // share of phones whose battery dies at their first failed send
  startTogether?: boolean; // all phones tick in step (worst case) instead of random phases
}

interface Attempt {
  t0: number;
  t1: number;
  phone: number;
  kind: "single" | "batch";
  n: number;
  outcome: SendResult["kind"];
  reason: string | null;
}

interface PhoneRecord {
  id: number;
  tripId: string;
  generated: { seq: number; t: number; offline: boolean }[];
  acked: Record<number, number>; // seq -> time the server accepted it
  dead: boolean;
  restarted: boolean;
  leftInOutbox: number;
}

const cfg: Config = JSON.parse(readFileSync(process.argv[2], "utf8"));
const OUT = process.argv[3];
const T0 = Date.now();
const now = () => Date.now() - T0;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const random = cfg.jitter ? Math.random : () => 1;
const attempts: Attempt[] = [];
let rr = 0;

function netDown(phone: number, mode?: Outage["mode"]): boolean {
  const t = now();
  return cfg.outages.some(
    (o) => t >= o.startMs && t < o.endMs && (!mode || o.mode === mode) && phone < Math.round(cfg.phones * (o.fraction ?? 1)),
  );
}

/** fetch through the simulated network, spread over the replicas like a load balancer. */
function phoneFetch(phone: number): typeof fetch {
  return (async (path: string | URL | Request, init?: RequestInit) => {
    if (netDown(phone)) {
      // A dead network doesn't answer: wait until the app's own timeout gives up.
      await new Promise((_, reject) => {
        const sig = init?.signal;
        if (sig?.aborted) return reject(new DOMException("aborted", "AbortError"));
        sig?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      });
    }
    const base = cfg.baseUrls[rr++ % cfg.baseUrls.length];
    return fetch(base + String(path), init);
  }) as typeof fetch;
}

/** Records every request and which check-ins the server accepted. */
class RecordingTransport implements Transport {
  constructor(private inner: Transport, private rec: PhoneRecord, private onFail: () => void) {}
  private log(t0: number, kind: Attempt["kind"], seqs: number[], r: SendResult) {
    attempts.push({
      t0, t1: now(), phone: this.rec.id, kind, n: seqs.length, outcome: r.kind,
      reason: r.kind === "retry" ? r.reason : r.kind === "invalid" ? r.message : null,
    });
    if (r.kind === "ok") for (const s of seqs) this.rec.acked[s] ??= now();
    if (r.kind === "retry") this.onFail();
    return r;
  }
  // A phone whose battery died sends nothing more (its retry timer may still fire).
  private static readonly DEAD: SendResult = { kind: "retry", reason: "phone dead" };
  async postCheckin(c: Checkin, o?: { keepalive?: boolean }) {
    if (this.rec.dead) return RecordingTransport.DEAD;
    const t0 = now();
    return this.log(t0, "single", [c.seq], await this.inner.postCheckin(c, o));
  }
  async postBatch(b: CheckinBatch) {
    if (this.rec.dead) return RecordingTransport.DEAD;
    const t0 = now();
    return this.log(t0, "batch", b.checkins.map((c) => c.seq), await this.inner.postBatch(b));
  }
  post(path: string, body: unknown) {
    return this.inner.post(path, body);
  }
}

function draft(rec: PhoneRecord, i: number) {
  return {
    tripId: rec.tripId,
    travellerId: `U-${rec.id}`,
    timestamp: new Date().toISOString(), // with milliseconds, for accurate delay measurement
    lat: 1.4 + i * 1e-5, lng: 103.9, accuracyM: 10, speedMps: 1.2, headingDeg: 90,
    stepIndex: Math.floor(i / 10), routeVersion: 3, batteryPct: 80, status: "active" as const,
  };
}

async function runOutboxPhone(id: number): Promise<PhoneRecord> {
  const rec: PhoneRecord = { id, tripId: `${cfg.tripPrefix}-${id}`, generated: [], acked: {}, dead: false, restarted: false, leftInOutbox: 0 };
  const dies = id < Math.round(cfg.phones * (cfg.dieOnFailureFraction ?? 0));
  let dbName = `phone-${cfg.name}-${id}`;
  let db: DB = await openCareRouteDB(dbName);
  const make = () => {
    const outbox = new Outbox(db);
    const transport = new RecordingTransport(
      new HttpTransport({ fetchImpl: phoneFetch(id), timeoutMs: cfg.timeoutMs }),
      rec,
      () => { if (dies) rec.dead = true; },
    );
    const sender = new Sender({
      outbox, transport, random,
      isOnline: () => !netDown(id, "known"),
      backoffBaseMs: cfg.backoffBaseMs, backoffCapMs: cfg.backoffCapMs,
      withLock: (fn) => fn(),
    });
    return { outbox, sender };
  };
  let { outbox, sender } = make();
  let wasKnownDown = false;
  let i = 0;
  const phase = cfg.startTogether ? 0 : Math.floor(Math.random() * cfg.heartbeatMs);
  await sleep(phase);
  const end = cfg.durationMs + cfg.drainMs;
  while (now() < end && !rec.dead) {
    const t = now();
    if (cfg.restartAtMs !== undefined && !rec.restarted && t >= cfg.restartAtMs) {
      // Tab closed and reopened: in-memory state is gone, IndexedDB stays (if persistent).
      sender.dispose();
      if (!cfg.persistent) {
        db.close();
        dbName += "-after-restart";
        db = await openCareRouteDB(dbName);
      }
      ({ outbox, sender } = make());
      rec.restarted = true;
      void sender.flush();
    }
    const knownDown = netDown(id, "known");
    if (wasKnownDown && !knownDown) void sender.retryNow({ jitterMs: cfg.reconnectJitterMs }); // 'online' event
    wasKnownDown = knownDown;
    if (t < cfg.durationMs) {
      const item = await outbox.enqueue(draft(rec, i++), { offline: knownDown });
      rec.generated.push({ seq: item.seq, t: now(), offline: netDown(id) });
      void sender.flush();
    }
    await sleep(cfg.heartbeatMs);
  }
  sender.dispose();
  rec.leftInOutbox = await outbox.count();
  return rec;
}

async function runNaivePhone(id: number): Promise<PhoneRecord> {
  const rec: PhoneRecord = { id, tripId: `${cfg.tripPrefix}-${id}`, generated: [], acked: {}, dead: false, restarted: false, leftInOutbox: 0 };
  const transport = new RecordingTransport(new HttpTransport({ fetchImpl: phoneFetch(id), timeoutMs: cfg.timeoutMs }), rec, () => {});
  let seq = 0;
  await sleep(cfg.startTogether ? 0 : Math.floor(Math.random() * cfg.heartbeatMs));
  while (now() < cfg.durationMs) {
    const c: Checkin = { ...draft(rec, seq), seq, bufferedOffline: false };
    rec.generated.push({ seq, t: now(), offline: netDown(id) });
    if (!netDown(id, "known")) void transport.postCheckin(c); // send once; if it fails, it's gone
    seq++;
    await sleep(cfg.heartbeatMs);
  }
  await sleep(Math.min(cfg.drainMs, cfg.timeoutMs + 500)); // let the last requests finish
  return rec;
}

async function main() {
  const run = cfg.kind === "naive" ? runNaivePhone : runOutboxPhone;
  const phones = await Promise.all(Array.from({ length: cfg.phones }, (_, id) => run(id)));
  writeFileSync(OUT, JSON.stringify({ config: cfg, startedAtEpochMs: T0, endedAtMs: now(), phones, attempts }));
  const gen = phones.reduce((a, p) => a + p.generated.length, 0);
  const acked = phones.reduce((a, p) => a + Object.keys(p.acked).length, 0);
  console.log(`${cfg.name}: ${cfg.phones} phones, ${gen} check-ins made, ${acked} acknowledged, ${attempts.length} requests`);
  process.exit(0);
}

void main();
