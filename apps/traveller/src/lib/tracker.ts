// Turns the phone's location into check-ins.
//
//  * GPS updates come from watchPosition and are only remembered; check-ins go out on a
//    fixed rhythm (every 10 s, contracts/README.md), so the Heartbeat Monitor sees a
//    steady pulse it can learn from, not bursts whenever GPS happens to fire.
//  * A check-in also goes out immediately when the traveller finishes a step, presses
//    help, or the app goes to the background or comes back.
//  * The heartbeat keeps going when the traveller stands still (same position again):
//    standing still is information too (the dwell-time detector needs it), and silence
//    must only ever mean "the phone can't reach us".
//  * Going to the background: browsers pause or slow timers and GPS for hidden pages
//    (iOS stops them). So before that happens the app sends one check-in with
//    status "backgrounded", using keepalive so it still gets out as the page is hidden.
//    The Heartbeat Monitor reads that status and can expect silence instead of raising
//    "no response" (a false alarm the caregiver would otherwise get every time the
//    traveller locks their phone).
import type { Outbox } from "./outbox";
import type { Sender } from "./sender";
import type { Session, TravellerStatus } from "../types";

export interface Fix {
  lat: number;
  lng: number;
  accuracyM: number | null;
  speedMps: number | null;
  headingDeg: number | null;
  at: number;
}

export type TrackerProblem = "location-denied" | "location-unavailable" | null;

export interface TrackerDeps {
  session: Session;
  outbox: Outbox;
  sender: Sender;
  geolocation?: Geolocation | null;
  intervalMs?: number;
  getProgress: () => { stepIndex: number | null; routeVersion: number | null };
  getBatteryPct?: () => number | null;
  isOnline?: () => boolean;
  isHidden?: () => boolean;
  now?: () => number;
  onProblem?: (p: TrackerProblem) => void;
  onFix?: (f: Fix) => void;
}

const num = (v: number | null | undefined, digits?: number): number | null => {
  if (v === null || v === undefined || Number.isNaN(v) || !Number.isFinite(v)) return null;
  return digits === undefined ? v : Number(v.toFixed(digits));
};

export function fixFromPosition(p: GeolocationPosition): Fix {
  const c = p.coords;
  const heading = num(c.heading);
  return {
    lat: Number(c.latitude.toFixed(6)),
    lng: Number(c.longitude.toFixed(6)),
    accuracyM: num(c.accuracy, 1),
    speedMps: c.speed === null || c.speed < 0 ? null : num(c.speed, 2),
    // Browsers report NaN heading when standing still; the contract wants null.
    headingDeg: heading === null ? null : Number((((heading % 360) + 360) % 360).toFixed(1)),
    at: p.timestamp,
  };
}

export class Tracker {
  private watchId: number | null = null;
  private interval: ReturnType<typeof setInterval> | null = null;
  private lastFix: Fix | null = null;
  private listeners: (() => void)[] = [];
  private d: Required<TrackerDeps>;

  constructor(deps: TrackerDeps) {
    this.d = {
      geolocation: typeof navigator !== "undefined" ? navigator.geolocation ?? null : null,
      intervalMs: 10_000,
      getBatteryPct: () => null,
      isOnline: () => (typeof navigator === "undefined" ? true : navigator.onLine),
      isHidden: () => typeof document !== "undefined" && document.visibilityState === "hidden",
      now: Date.now,
      onProblem: () => {},
      onFix: () => {},
      ...deps,
    } as Required<TrackerDeps>;
  }

  get fix(): Fix | null {
    return this.lastFix;
  }

  start(): void {
    const geo = this.d.geolocation;
    if (!geo) {
      this.d.onProblem("location-unavailable");
    } else {
      this.watchId = geo.watchPosition(
        (p) => this.acceptFix(fixFromPosition(p)),
        (err) => this.d.onProblem(err.code === err.PERMISSION_DENIED ? "location-denied" : "location-unavailable"),
        { enableHighAccuracy: true, maximumAge: 5_000, timeout: 20_000 },
      );
    }
    this.interval = setInterval(() => void this.checkInNow(), this.d.intervalMs);

    const on = (target: EventTarget, type: string, fn: () => void) => {
      target.addEventListener(type, fn);
      this.listeners.push(() => target.removeEventListener(type, fn));
    };
    if (typeof document !== "undefined") {
      on(document, "visibilitychange", () => void this.onVisibilityChange());
      // pagehide fires on iOS where visibilitychange sometimes doesn't, e.g. closing the tab.
      on(window, "pagehide", () => void this.checkInNow({ keepalive: true, status: "backgrounded" }));
    }
    if (typeof window !== "undefined") {
      on(window, "online", () => void this.d.sender.retryNow());
    }
  }

  stop(): void {
    if (this.watchId !== null) this.d.geolocation?.clearWatch(this.watchId);
    if (this.interval !== null) clearInterval(this.interval);
    this.listeners.forEach((off) => off());
    this.watchId = null;
    this.interval = null;
    this.listeners = [];
  }

  /** Feed a position in (from watchPosition, or from a test). */
  acceptFix(f: Fix): void {
    const first = this.lastFix === null;
    this.lastFix = f;
    this.d.onProblem(null);
    this.d.onFix(f);
    if (first) void this.checkInNow(); // don't make the caregiver wait 10 s for the first one
  }

  /** Make a check-in from the latest position and send it. Returns false if there's no position yet. */
  async checkInNow(opts: { keepalive?: boolean; status?: TravellerStatus } = {}): Promise<boolean> {
    const f = this.lastFix;
    if (!f) return false;
    const { stepIndex, routeVersion } = this.d.getProgress();
    const status: TravellerStatus = opts.status ?? (this.d.isHidden() ? "backgrounded" : "active");
    await this.d.outbox.enqueue(
      {
        tripId: this.d.session.tripId,
        travellerId: this.d.session.travellerId,
        timestamp: new Date(this.d.now()).toISOString().replace(/\.\d{3}Z$/, "Z"),
        lat: f.lat,
        lng: f.lng,
        accuracyM: f.accuracyM,
        speedMps: f.speedMps,
        headingDeg: f.headingDeg,
        stepIndex,
        routeVersion,
        batteryPct: this.d.getBatteryPct(),
        status,
      },
      { offline: !this.d.isOnline() },
    );
    await this.d.sender.flush({ keepalive: opts.keepalive });
    return true;
  }

  private async onVisibilityChange(): Promise<void> {
    if (this.d.isHidden()) {
      await this.checkInNow({ keepalive: true });
    } else {
      await this.checkInNow();
      await this.d.sender.retryNow(); // the backoff timer may have been frozen while hidden
    }
  }
}
