import { Outbox } from "../src/lib/outbox";
import { draft, freshDb, makeOutbox, T0 } from "./helpers";

describe("Outbox", () => {
  it("numbers check-ins from the current time in seconds, then counts up", async () => {
    const { outbox } = await makeOutbox();
    const a = await outbox.enqueue(draft(), { offline: false });
    const b = await outbox.enqueue(draft(), { offline: false });
    expect(a.seq).toBe(Math.floor(T0 / 1000));
    expect(b.seq).toBe(a.seq + 1);
    expect(a.checkin.seq).toBe(a.seq);
  });

  it("keeps counting after the app is reloaded (no repeated seq)", async () => {
    const db = await freshDb();
    const first = await new Outbox(db, () => T0).enqueue(draft(), { offline: false });
    // A "reload": a new Outbox on the same storage, much later.
    const after = await new Outbox(db, () => T0 + 3_600_000).enqueue(draft(), { offline: false });
    expect(after.seq).toBe(first.seq + 1);
  });

  it("never repeats a seq when many check-ins are made at once", async () => {
    const { outbox } = await makeOutbox();
    const items = await Promise.all(Array.from({ length: 50 }, () => outbox.enqueue(draft(), { offline: false })));
    expect(new Set(items.map((i) => i.seq)).size).toBe(50);
    expect(await outbox.count()).toBe(50);
  });

  it("counts separately per trip", async () => {
    const { outbox } = await makeOutbox();
    const a = await outbox.enqueue(draft({ tripId: "T-001" }), { offline: false });
    const b = await outbox.enqueue(draft({ tripId: "T-002" }), { offline: false });
    const c = await outbox.enqueue(draft({ tripId: "T-001" }), { offline: false });
    expect(c.seq).toBe(a.seq + 1);
    expect(b.seq).toBe(a.seq);
  });

  it("returns oldest first, one trip at a time", async () => {
    const { outbox } = await makeOutbox();
    for (let i = 0; i < 3; i++) await outbox.enqueue(draft({ tripId: "T-001" }), { offline: false });
    await outbox.enqueue(draft({ tripId: "T-002" }), { offline: false });
    const peeked = await outbox.peek(10);
    expect(peeked.map((p) => p.tripId)).toEqual(["T-001", "T-001", "T-001"]);
    expect(peeked.map((p) => p.seq)).toEqual([...peeked.map((p) => p.seq)].sort((x, y) => x - y));
    expect(await outbox.peek(2)).toHaveLength(2);
  });

  it("marks check-ins made offline as buffered", async () => {
    const { outbox } = await makeOutbox();
    const item = await outbox.enqueue(draft(), { offline: true });
    expect(item.buffered).toBe(true);
    expect(item.checkin.bufferedOffline).toBe(true);
  });

  it("markBuffered flags late check-ins and counts attempts", async () => {
    const { outbox } = await makeOutbox();
    const item = await outbox.enqueue(draft(), { offline: false });
    await outbox.markBuffered([item], true);
    const [after] = await outbox.peek(1);
    expect(after.buffered).toBe(true);
    expect(after.checkin.bufferedOffline).toBe(true);
    expect(after.attempts).toBe(1);
  });

  it("drops the oldest check-ins past its size limit", async () => {
    const db = await freshDb();
    const outbox = new Outbox(db, () => T0, 5);
    for (let i = 0; i < 8; i++) await outbox.enqueue(draft(), { offline: true });
    const left = await outbox.peek(10);
    expect(left).toHaveLength(5);
    expect(left[0].seq).toBe(Math.floor(T0 / 1000) + 3);
    expect(outbox.dropped).toBe(3);
  });
});
