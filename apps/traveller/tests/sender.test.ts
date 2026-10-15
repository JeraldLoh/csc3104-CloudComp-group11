import { backoffDelay, Sender } from "../src/lib/sender";
import { draft, FakeTransport, makeOutbox, ManualTimers, noLock } from "./helpers";

async function setup(opts: { online?: () => boolean; random?: () => number } = {}) {
  const { outbox } = await makeOutbox();
  const transport = new FakeTransport();
  const timers = new ManualTimers();
  const sender = new Sender({
    outbox, transport, withLock: noLock,
    isOnline: opts.online ?? (() => true),
    random: opts.random ?? (() => 0.5),
    setTimer: timers.set, clearTimer: timers.clear,
  });
  return { outbox, transport, timers, sender };
}

describe("Sender", () => {
  it("sends an up-to-date check-in on its own and removes it", async () => {
    const { outbox, transport, sender } = await setup();
    await outbox.enqueue(draft(), { offline: false });
    await sender.flush();
    expect(transport.calls).toHaveLength(1);
    expect(transport.calls[0].kind).toBe("single");
    expect(await outbox.count()).toBe(0);
    expect(sender.state.sent).toBe(1);
  });

  it("offline: sends nothing, keeps everything, then sends one ordered batch when back online", async () => {
    let online = false;
    const { outbox, transport, sender } = await setup({ online: () => online });
    for (let i = 0; i < 5; i++) {
      await outbox.enqueue(draft({ stepIndex: i }), { offline: false });
      await sender.flush();
    }
    expect(transport.calls).toHaveLength(0);
    expect(await outbox.count()).toBe(5);

    online = true;
    await sender.retryNow();
    expect(transport.calls).toHaveLength(1);
    const call = transport.calls[0];
    expect(call.kind).toBe("batch");
    if (call.kind !== "batch") return;
    const seqs = call.batch.checkins.map((c) => c.seq);
    expect(seqs).toEqual([...seqs].sort((a, b) => a - b));
    expect(call.batch.checkins.every((c) => c.bufferedOffline)).toBe(true);
    expect(call.batch).toMatchObject({ tripId: "T-001", travellerId: "U-042" });
    expect(call.batch.checkins[0]).not.toHaveProperty("tripId");
    expect(await outbox.count()).toBe(0);
  });

  it("server error: keeps the check-in, marks it late, retries after a backoff", async () => {
    const { outbox, transport, timers, sender } = await setup();
    transport.respond = () => ({ kind: "retry", reason: "HTTP 503" });
    await outbox.enqueue(draft(), { offline: false });
    await sender.flush();

    expect(await outbox.count()).toBe(1);
    const [kept] = await outbox.peek(1);
    expect(kept.buffered).toBe(true);
    expect(kept.attempts).toBe(1);
    expect(sender.state.consecutiveFailures).toBe(1);
    expect(timers.pending).toHaveLength(1);
    expect(timers.pending[0].ms).toBe(500); // random 0.5 x base 1000

    transport.respond = (c) => ({ kind: "ok", body: { accepted: c.kind === "batch" ? c.batch.checkins.length : 1, duplicates: 0 } });
    timers.fireAll();
    await sender.flush();
    expect(await outbox.count()).toBe(0);
    expect(sender.state.consecutiveFailures).toBe(0);
    // The retry went as a batch with bufferedOffline: true, so the server doesn't count it as a heartbeat.
    expect(transport.calls[1].kind).toBe("batch");
  });

  it("never loses or reorders check-ins through a run of failures", async () => {
    const { outbox, transport, timers, sender } = await setup();
    let failuresLeft = 4;
    transport.respond = (c) =>
      failuresLeft-- > 0
        ? { kind: "retry", reason: "timed out" }
        : { kind: "ok", body: { accepted: c.kind === "batch" ? c.batch.checkins.length : 1, duplicates: 0 } };
    const made: number[] = [];
    for (let i = 0; i < 12; i++) {
      made.push((await outbox.enqueue(draft(), { offline: false })).seq);
      await sender.flush();
      timers.fireAll();
      await sender.flush();
    }
    const delivered = transport.calls
      .filter((_, i) => i >= 4)
      .flatMap((c) => (c.kind === "batch" ? c.batch.checkins.map((x) => x.seq) : c.kind === "single" ? [c.checkin.seq] : []));
    expect(delivered).toEqual(made);
    expect(await outbox.count()).toBe(0);
  });

  it("rejected (422) check-in is dropped so it can't block the rest", async () => {
    const { outbox, transport, sender } = await setup();
    const bad = await outbox.enqueue(draft(), { offline: false });
    transport.respond = (c) =>
      c.kind === "single" && c.checkin.seq === bad.seq
        ? { kind: "invalid", status: 422, message: "bad" }
        : { kind: "ok", body: { accepted: 1, duplicates: 0 } };
    await sender.flush();
    await outbox.enqueue(draft(), { offline: false });
    await sender.flush();
    expect(await outbox.count()).toBe(0);
    expect(sender.state.rejected).toBe(1);
    expect(sender.state.sent).toBe(1);
  });

  it("a rejected batch is retried one by one, so only the bad check-in is lost", async () => {
    let online = false;
    const { outbox, transport, sender } = await setup({ online: () => online });
    const items = [];
    for (let i = 0; i < 4; i++) items.push(await outbox.enqueue(draft(), { offline: true }));
    const badSeq = items[2].seq;
    transport.respond = (c) => {
      if (c.kind === "batch") return { kind: "invalid", status: 422, message: "one bad item" };
      if (c.kind === "single" && c.checkin.seq === badSeq) return { kind: "invalid", status: 422, message: "bad" };
      return { kind: "ok", body: { accepted: 1, duplicates: 0 } };
    };
    online = true;
    await sender.retryNow();
    expect(await outbox.count()).toBe(0);
    expect(sender.state.rejected).toBe(1);
    expect(sender.state.sent).toBe(3);
  });

  it("401: stops sending until the traveller logs in again", async () => {
    const { outbox, transport, timers, sender } = await setup();
    transport.respond = () => ({ kind: "unauthorized", status: 401 });
    await outbox.enqueue(draft(), { offline: false });
    await sender.flush();
    expect(sender.state.authRequired).toBe(true);
    expect(timers.pending).toHaveLength(0); // no pointless retry loop
    await outbox.enqueue(draft(), { offline: false });
    await sender.flush();
    expect(transport.calls).toHaveLength(1);

    transport.respond = (c) => ({ kind: "ok", body: { accepted: c.kind === "batch" ? c.batch.checkins.length : 1, duplicates: 0 } });
    await sender.resumeAfterLogin();
    expect(await outbox.count()).toBe(0);
  });

  it("only one request at a time, even when flush is called repeatedly", async () => {
    const { outbox, transport, sender } = await setup();
    const flushes: Promise<void>[] = [];
    for (let i = 0; i < 20; i++) {
      await outbox.enqueue(draft(), { offline: false });
      flushes.push(sender.flush());
    }
    await Promise.all(flushes);
    await sender.flush();
    expect(transport.maxInFlight).toBe(1);
    expect(await outbox.count()).toBe(0);
    const seqs = transport.seqs();
    expect(seqs).toEqual([...seqs].sort((a, b) => a - b));
    expect(new Set(seqs).size).toBe(20);
  });

  it("a big backlog goes in batches of 100, oldest first", async () => {
    let online = false;
    const { outbox, transport, sender } = await setup({ online: () => online });
    for (let i = 0; i < 250; i++) await outbox.enqueue(draft(), { offline: true });
    online = true;
    await sender.retryNow();
    const sizes = transport.calls.map((c) => (c.kind === "batch" ? c.batch.checkins.length : 1));
    expect(sizes).toEqual([100, 100, 50]);
    const seqs = transport.seqs();
    expect(seqs).toEqual([...seqs].sort((a, b) => a - b));
  });

  it("uses keepalive for the check-in sent as the app goes to the background", async () => {
    const { outbox, transport, sender } = await setup();
    await outbox.enqueue(draft({ status: "backgrounded" }), { offline: false });
    await sender.flush({ keepalive: true });
    expect(transport.calls[0]).toMatchObject({ kind: "single", keepalive: true });
  });

  it("server reporting duplicates (a retry after a lost reply) is still success", async () => {
    const { outbox, transport, sender } = await setup();
    transport.respond = () => ({ kind: "ok", body: { accepted: 0, duplicates: 1 } });
    await outbox.enqueue(draft(), { offline: false });
    await sender.flush();
    expect(await outbox.count()).toBe(0);
    expect(sender.state.duplicates).toBe(1);
  });
});

describe("backoffDelay (exponential, full jitter)", () => {
  it("doubles the ceiling each failure, up to the cap", () => {
    const max = (n: number) => backoffDelay(n, () => 0.999999, 1000, 60_000);
    expect([1, 2, 3, 4, 5, 6, 7, 8].map(max)).toEqual([999, 1999, 3999, 7999, 15999, 31999, 59999, 59999]);
  });
  it("is random between a small floor and the ceiling, so phones don't retry in lockstep", () => {
    expect(backoffDelay(5, () => 0, 1000, 60_000)).toBe(250);
    const samples = new Set(Array.from({ length: 50 }, () => backoffDelay(6, Math.random, 1000, 60_000)));
    expect(samples.size).toBeGreaterThan(40);
  });
});
