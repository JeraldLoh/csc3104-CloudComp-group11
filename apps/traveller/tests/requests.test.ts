import type { SendResult } from "../src/lib/api";
import { RequestQueue } from "../src/lib/requests";
import { FakeTransport, freshDb } from "./helpers";

async function setup() {
  const db = await freshDb();
  const transport = new FakeTransport();
  const q = new RequestQueue(db, transport, { random: () => 0.5, baseMs: 60_000 }); // long backoff: only our flushes run
  return { db, transport, q };
}

describe("RequestQueue", () => {
  it("sends and removes a request", async () => {
    const { transport, q } = await setup();
    await q.add("trip start", "/api/trips/T-001/start", {});
    expect(transport.calls).toEqual([{ kind: "post", path: "/api/trips/T-001/start", body: {} }]);
    expect(q.state.pending).toHaveLength(0);
    expect(q.state.lastDone?.label).toBe("trip start");
    q.dispose();
  });

  it("a request that keeps failing does not block help (no head-of-line blocking)", async () => {
    const { transport, q } = await setup();
    transport.respond = (c) =>
      c.kind === "post" && c.path.endsWith("/start") ? { kind: "retry", reason: "HTTP 502" } : { kind: "ok", body: { incidentId: "I-1" } };
    await q.add("trip start", "/api/trips/T-001/start", {});
    await q.add("help", "/api/help", { tripId: "T-001", lat: 1.4, lng: 103.9 });
    expect(q.state.lastDone?.label).toBe("help");
    expect(q.state.pending.map((p) => p.label)).toEqual(["trip start"]);
    q.dispose();
  });

  it("help goes before anything else that is waiting", async () => {
    const { transport, q } = await setup();
    transport.respond = () => ({ kind: "retry", reason: "offline" });
    await q.add("trip start", "/a", {});
    await q.add("trip end", "/b", {});
    await q.add("help", "/api/help", {});
    transport.calls = [];
    transport.respond = () => ({ kind: "ok", body: {} });
    await q.retryNow();
    expect(transport.calls.map((c) => (c.kind === "post" ? c.path : ""))).toEqual(["/api/help", "/a", "/b"]);
    q.dispose();
  });

  it("keeps a failed request and counts the attempt, so the help screen can say 'call now'", async () => {
    const { transport, q } = await setup();
    transport.respond = () => ({ kind: "retry", reason: "no signal" });
    await q.add("help", "/api/help", {});
    expect(q.state.pending).toHaveLength(1);
    expect(q.state.pending[0].attempts).toBe(1);
    q.dispose();
  });

  it("drops a request the server says is invalid instead of retrying forever", async () => {
    const { transport, q } = await setup();
    transport.respond = () => ({ kind: "invalid", status: 404, message: "Trip T-9 not found" });
    await q.add("trip start", "/api/trips/T-9/start", {});
    expect(q.state.pending).toHaveLength(0);
    expect(q.state.lastError).toContain("not found");
    q.dispose();
  });

  it("survives a reload: requests left in storage are sent by the next app instance", async () => {
    const { db, transport } = await setup();
    const first = new RequestQueue(db, transport, { baseMs: 60_000 });
    transport.respond = () => ({ kind: "retry", reason: "offline" });
    await first.add("help", "/api/help", {});
    first.dispose();

    transport.respond = () => ({ kind: "ok", body: {} });
    const second = new RequestQueue(db, transport, { baseMs: 60_000 });
    await second.flush();
    expect(second.state.pending).toHaveLength(0);
    expect(second.state.lastDone?.label).toBe("help");
    second.dispose();
  });

  it("disposed while a request is out (database closed): stops cleanly, nothing more sent", async () => {
    const { db, transport } = await setup();
    const q = new RequestQueue(db, transport, { random: () => 0.5, baseMs: 1 });
    let answer!: (r: SendResult) => void;
    transport.respond = () => new Promise<SendResult>((r) => (answer = r));
    const adding = q.add("help", "/api/help", {});
    await vi.waitFor(() => expect(transport.calls).toHaveLength(1));
    q.dispose();
    db.close();
    answer({ kind: "retry", reason: "timeout" });
    await adding;
    await new Promise((r) => setTimeout(r, 20)); // a 1 ms backoff would have fired by now
    expect(transport.calls).toHaveLength(1);
    await q.retryNow();
    expect(transport.calls).toHaveLength(1);
  });
});
