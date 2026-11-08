import { fixFromPosition, Tracker, type Fix } from "../src/lib/tracker";
import { Sender } from "../src/lib/sender";
import { FakeTransport, makeOutbox, ManualTimers, noLock, T0 } from "./helpers";

const fix: Fix = { lat: 1.4052, lng: 103.9024, accuracyM: 8, speedMps: 1.2, headingDeg: 90, at: T0 };

async function setup(over: { hidden?: boolean; online?: boolean } = {}) {
  const { outbox } = await makeOutbox();
  const transport = new FakeTransport();
  const timers = new ManualTimers();
  const sender = new Sender({ outbox, transport, withLock: noLock, setTimer: timers.set, clearTimer: timers.clear, isOnline: () => over.online ?? true });
  let step = 2;
  const tracker = new Tracker({
    session: { tripId: "T-001", travellerId: "U-042" },
    outbox, sender, geolocation: null,
    getProgress: () => ({ stepIndex: step, routeVersion: 3 }),
    getBatteryPct: () => 71,
    isHidden: () => over.hidden ?? false,
    isOnline: () => over.online ?? true,
    now: () => T0,
  });
  return { tracker, transport, outbox, setStep: (s: number) => (step = s) };
}

const flushAsync = () => new Promise((r) => setTimeout(r, 20));

describe("Tracker", () => {
  it("sends nothing before the first GPS fix", async () => {
    const { tracker, transport } = await setup();
    expect(await tracker.checkInNow()).toBe(false);
    expect(transport.calls).toHaveLength(0);
  });

  it("sends a check-in straight away on the first fix, in contract format", async () => {
    const { tracker, transport } = await setup();
    tracker.acceptFix(fix);
    await flushAsync();
    expect(transport.calls).toHaveLength(1);
    const call = transport.calls[0];
    if (call.kind !== "single") throw new Error("expected single");
    expect(call.checkin).toEqual({
      tripId: "T-001", travellerId: "U-042", seq: Math.floor(T0 / 1000),
      timestamp: "2026-10-16T08:00:00Z",
      lat: 1.4052, lng: 103.9024, accuracyM: 8, speedMps: 1.2, headingDeg: 90,
      stepIndex: 2, routeVersion: 3, batteryPct: 71, status: "active", bufferedOffline: false,
    });
  });

  it("later fixes don't each trigger a check-in (the heartbeat sets the rhythm)", async () => {
    const { tracker, transport } = await setup();
    tracker.acceptFix(fix);
    tracker.acceptFix({ ...fix, lat: 1.406 });
    tracker.acceptFix({ ...fix, lat: 1.407 });
    await flushAsync();
    expect(transport.calls).toHaveLength(1);
    await tracker.checkInNow();
    const last = transport.calls.at(-1)!;
    expect(last.kind === "single" && last.checkin.lat).toBe(1.407);
  });

  it("reports the current step", async () => {
    const { tracker, transport, setStep } = await setup();
    tracker.acceptFix(fix);
    await flushAsync();
    setStep(5);
    await tracker.checkInNow();
    const last = transport.calls.at(-1)!;
    expect(last.kind === "single" && last.checkin.stepIndex).toBe(5);
  });

  it("marks check-ins 'backgrounded' when the app is hidden", async () => {
    const { tracker, transport } = await setup({ hidden: true });
    tracker.acceptFix(fix);
    await flushAsync();
    const c = transport.calls[0];
    expect(c.kind === "single" && c.checkin.status).toBe("backgrounded");
  });

  it("check-ins made with no signal are stored as buffered, not sent", async () => {
    const { tracker, transport, outbox } = await setup({ online: false });
    tracker.acceptFix(fix);
    await flushAsync();
    await tracker.checkInNow();
    expect(transport.calls).toHaveLength(0);
    const kept = await outbox.peek(10);
    expect(kept).toHaveLength(2);
    expect(kept.every((k) => k.checkin.bufferedOffline)).toBe(true);
  });

  it("ticks every interval", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    try {
      const { tracker } = await setup();
      const spy = vi.spyOn(tracker, "checkInNow").mockResolvedValue(true);
      tracker.start();
      vi.advanceTimersByTime(35_000);
      expect(spy).toHaveBeenCalledTimes(3);
      tracker.stop();
      vi.advanceTimersByTime(30_000);
      expect(spy).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("fixFromPosition", () => {
  const pos = (coords: Partial<GeolocationCoordinates>): GeolocationPosition =>
    ({
      timestamp: T0,
      coords: { latitude: 1.40523456789, longitude: 103.90241234, accuracy: 7.456, altitude: null, altitudeAccuracy: null, heading: null, speed: null, ...coords },
    }) as GeolocationPosition;

  it("rounds to 6 decimal places (~10 cm) and 1 dp accuracy", () => {
    expect(fixFromPosition(pos({}))).toMatchObject({ lat: 1.405235, lng: 103.902412, accuracyM: 7.5 });
  });
  it("turns the browser's NaN heading (standing still) into null", () => {
    expect(fixFromPosition(pos({ heading: NaN })).headingDeg).toBeNull();
  });
  it("keeps heading in 0-360 and drops negative speed", () => {
    expect(fixFromPosition(pos({ heading: 360, speed: -1 }))).toMatchObject({ headingDeg: 0, speedMps: null });
  });
});
