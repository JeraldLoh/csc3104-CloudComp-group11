// Demo mode (?demo=1): a sample route pack and a pretend GPS that walks along it.
// Lets you show the whole flow on a laptop with no GPS, and before the Journey Service
// (issue 6.1) exists. The pack follows contracts/examples/route-pack.json.
import type { RoutePack } from "../types";
import type { Fix } from "./tracker";

export const DEMO_PACK: RoutePack = {
  packVersion: "R-007-v3",
  routeId: "R-007",
  routeVersion: 3,
  travellerName: "Alex",
  steps: [
    { stepIndex: 0, mode: "walk", easyRead: "Walk to the bus stop near the playground.", lat: 1.4052, lng: 103.9024, underground: false, photoUrl: "demo-media/step0.svg", audioUrl: null, backup: null },
    { stepIndex: 1, mode: "bus", easyRead: "Get on bus number 190. Tap your card.", lat: 1.3935, lng: 103.8962, underground: false, photoUrl: "demo-media/step1.svg", audioUrl: null, backup: "If bus 190 does not come in 20 minutes, take bus 83 from the same stop." },
    { stepIndex: 2, mode: "bus", easyRead: "Stay on the bus. Get off after 6 stops, at Block 221.", lat: 1.3935, lng: 103.8962, underground: false, photoUrl: "demo-media/step2.svg", audioUrl: null, backup: null },
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
        ? { ...s, easyRead: "Stay on the bus. Get off after 5 stops, at Block 221." }
        : s,
  ),
  generatedAt: "2026-10-16T08:20:00Z",
};

/** Moves along the pack's path, a little each call. */
export class DemoWalker {
  private t = 0;
  constructor(private path: [number, number][], private stepPerTick = 0.04) {}

  next(now: number): Fix {
    const segs = this.path.length - 1;
    const pos = Math.min(this.t, 0.9999) * segs;
    const i = Math.floor(pos);
    const f = pos - i;
    const [a, b] = [this.path[i], this.path[i + 1]];
    this.t = Math.min(1, this.t + this.stepPerTick);
    const heading = (Math.atan2(b[1] - a[1], b[0] - a[0]) * 180) / Math.PI;
    return {
      lat: Number((a[0] + (b[0] - a[0]) * f).toFixed(6)),
      lng: Number((a[1] + (b[1] - a[1]) * f).toFixed(6)),
      accuracyM: 10,
      speedMps: 1.3,
      headingDeg: Number((((heading % 360) + 360) % 360).toFixed(1)),
      at: now,
    };
  }
}
