// Demo mode (?demo=1): a sample route pack and a pretend GPS that follows it.
// Lets you show the whole flow on a laptop with no GPS, and before the Journey Service
// (issue 6.1) exists. The pack follows contracts/examples/route-pack.json.
import type { PackStop, RoutePack } from "../types";
import type { Fix } from "./tracker";

// The bus ride of step 2, from the stop at the playground to Block 221: 7 stops, so 6 to ride
const RIDE: [number, number][] = [[1.4052, 103.9024], [1.399, 103.899], [1.3935, 103.8962]];
const DEMO_STOPS: PackStop[] = [
  ["Punggol Interchange", 0], ["Block 268", 0.17], ["Block 261", 0.33], ["Waterway Point", 0.5], ["Block 230", 0.66],
  ["Block 222", 0.83], ["Block 221", 1],
].map(([name, f]) => {
  const [lat, lng] = pointAlong(RIDE, f as number);
  return { name: name as string, lat, lng };
});

export const DEMO_PACK: RoutePack = {
  packVersion: "R-007-v3",
  routeId: "R-007",
  routeVersion: 3,
  travellerName: "Alex",
  steps: [
    { stepIndex: 0, mode: "walk", easyRead: "Walk to the bus stop near the playground.", lat: 1.4052, lng: 103.9024, underground: false, photoUrl: "demo-media/step0.svg", audioUrl: null, backup: null },
    // Getting on: this step ends where it starts, at the bus stop
    { stepIndex: 1, mode: "bus", easyRead: "Get on bus number 190. Tap your card.", lat: 1.4052, lng: 103.9024, underground: false, photoUrl: "demo-media/step1.svg", audioUrl: null, backup: "If bus 190 does not come in 20 minutes, take bus 83 from the same stop." },
    {
      stepIndex: 2, mode: "bus", easyRead: "Stay on the bus. Count 6 stops. Look out for Waterway Point on your left. Press the bell. Get off at Block 221.",
      lat: 1.3935, lng: 103.8962, underground: false, photoUrl: "demo-media/step2.svg", audioUrl: null, backup: null,
      stops: DEMO_STOPS,
      // Like the Journey Service's landmarks: one with its own photo, one already in the step photo. With their
      // positions, so the guide shows each as the bus gets near (src/lib/guide.ts).
      landmarks: [
        { name: "Waterway Point", photoUrl: "demo-media/waterway-point.svg", lat: 1.3995, lng: 103.8975, seenFrom: [1.399, 103.899] },
        { name: "Block 221", photoUrl: null, sameView: "step", lat: 1.3936, lng: 103.8963 },
      ],
    },
    { stepIndex: 3, mode: "walk", easyRead: "Walk to the clinic. It has a green sign.", lat: 1.392, lng: 103.895, underground: false, photoUrl: "demo-media/step3.svg", audioUrl: null, backup: null },
  ],
  path: [[1.404, 103.901], [1.4052, 103.9024], [1.399, 103.899], [1.3935, 103.8962], [1.392, 103.895]],
  caregiver: { name: "Sam", phone: "+6590000000" },
  messages: {
    somethingChanged: "Something has changed. Stay where you are. Your caregiver has been told.",
    offRoute: "You may be going the wrong way. Stop and look at the next picture.",
    help: "Press the big red button if you need help.",
  },
  generatedAt: "2026-10-10T14:05:00Z",
};

/** The same trip after the caregiver changed it: bus 190 replaced by bus 83 (debug panel button). */
export const DEMO_PACK_CHANGED: RoutePack = {
  ...DEMO_PACK,
  packVersion: "R-007-v4",
  routeVersion: 4,
  steps: DEMO_PACK.steps.map((s) =>
    s.stepIndex === 1
      ? { ...s, easyRead: "Get on bus number 83. Tap your card.", photoUrl: "demo-media/step1-bus83.svg", backup: null }
      : s.stepIndex === 2
        ? { ...s, easyRead: "Stay on the bus. Count 5 stops. Press the bell. Get off at Block 221.",
            stops: DEMO_STOPS.filter((_, i) => i !== 3), landmarks: [{ name: "Block 221", photoUrl: null, sameView: "step" }] }
        : s,
  ),
  generatedAt: "2026-10-16T08:20:00Z",
};

/**
 * Moves along the step the traveller is on, a little each call: from where the step before ended, through the ride's
 * stops, to where this step ends, then waits there. Sped up for the demo (a bus ride takes about 30 s).
 */
export class DemoWalker {
  private stepIndex = -1;
  private line: [number, number][] = [];
  private gone = 0;

  constructor(private getRoute: () => { pack: RoutePack; stepIndex: number }, private tickS = 3) {}

  next(now: number): Fix {
    const { pack, stepIndex } = this.getRoute();
    const step = pack.steps[stepIndex] ?? pack.steps[0];
    if (stepIndex !== this.stepIndex) {
      this.stepIndex = stepIndex;
      this.gone = 0;
      const before = stepIndex > 0 ? pack.steps[stepIndex - 1] : null;
      const from: [number, number] = before ? [before.lat, before.lng] : pack.path[0];
      const stops = (step.stops ?? []).filter((st) => st.lat !== null && st.lng !== null).map((st) => [st.lat, st.lng] as [number, number]);
      this.line = [from, ...stops, [step.lat, step.lng]];
    }
    const perTick = step.mode === "walk" ? 25 : 150;        // metres per call
    const total = length(this.line);
    const f = total === 0 ? 1 : Math.min(1, this.gone / total);
    const [lat, lng] = pointAlong(this.line, f);
    const [nlat, nlng] = pointAlong(this.line, Math.min(1, f + 0.01));
    this.gone += perTick;
    const heading = (Math.atan2(nlng - lng, nlat - lat) * 180) / Math.PI;
    return {
      lat: Number(lat.toFixed(6)),
      lng: Number(lng.toFixed(6)),
      accuracyM: 10,
      speedMps: f < 1 ? Number((perTick / this.tickS).toFixed(1)) : 0,
      headingDeg: Number((((heading % 360) + 360) % 360).toFixed(1)),
      at: now,
    };
  }
}

function flatMetres(a: [number, number], b: [number, number]): number {
  return Math.hypot((b[0] - a[0]) * 110_574, (b[1] - a[1]) * 111_320 * Math.cos((a[0] * Math.PI) / 180));
}

function length(line: [number, number][]): number {
  let m = 0;
  for (let i = 1; i < line.length; i++) m += flatMetres(line[i - 1], line[i]);
  return m;
}

/** The point a fraction `f` (0 to 1) of the way along a line. */
function pointAlong(line: [number, number][], f: number): [number, number] {
  const total = length(line);
  let left = f * total;
  for (let i = 1; i < line.length; i++) {
    const seg = flatMetres(line[i - 1], line[i]);
    if (left <= seg || i === line.length - 1) {
      const t = seg === 0 ? 0 : Math.min(1, left / seg);
      const [a, b] = [line[i - 1], line[i]];
      return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
    }
    left -= seg;
  }
  return line[line.length - 1];
}
