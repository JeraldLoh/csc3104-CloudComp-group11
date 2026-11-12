// One thing at a time: which of a step's lines to show now, and what to tell the traveller as they go.
//
// A bus step's wording is up to nine short lines ("Find bus stop 65009 ...", "Raise your hand for bus 190",
// "Tap your card to get on", "After Block 301, press the bell", "Get off at Block 221", ...). Shown together, a
// traveller who finds reading hard has to hold all of them in mind for the whole ride. Instead, using the GPS the
// app already reads (tracker.ts), each line comes up when it is needed:
//
//   waiting      the lines up to getting on ("... Tap your card to get on")
//   on the bus   the rest of the ride's lines, minus the ones below; a big "5 more stops" that counts down as the
//                bus passes each stop (passing it by GPS, so a stop the bus doesn't stop at still counts: the
//                reason the Journey Service's wording avoids asking the traveller to count)
//   near a landmark   its line and photo ("Look out for Oasis Terraces on your right")
//   2 stops left      the bell line ("After Block 301, press the bell")
//   1 stop left       "Press the bell now", and the get-off lines
//   at the stop       "Get off here"
//
// Walk steps only hold back their landmark lines until the traveller is near. Train steps are shown whole (no GPS
// underground). Nothing is ever only hidden: without a recent GPS fix, without the stops' positions, or for wording
// this can't follow, the step is shown whole as before, and "Show all" on the card always shows every line.
//
// Everything here runs on the phone from the route pack, so it works with no signal. Pure functions, tested in
// tests/guide.test.ts; useGuide.ts runs them on each GPS fix and reads the new prompts aloud.
import { easyReadPoints } from "./easyRead";
import type { Fix } from "./tracker";
import type { RoutePackStep } from "../types";

type LatLng = [number, number];

export const GUIDE = {
  /** Fixes less sure than this are ignored. */
  maxAccuracyM: 75,
  /** No usable fix for this long: show the whole step. */
  freshMs: 45_000,
  /** Further than this from the line through the stops: not on this ride, ignored. */
  corridorM: 200,
  /** This far past the stop they got on at: on the bus. */
  rideStartM: 120,
  /** This far past a stop: it is behind them. */
  passedM: 25,
  /** This close (along the ride) to the last stop: get off. */
  arriveM: 60,
  /** Faster than this between two fixes: a GPS jump, not the bus. Ignored. */
  maxSpeedMps: 35,
  /** How far ahead a landmark comes up, on a bus and on foot, and how much further it stays. */
  landmarkBusM: 300,
  landmarkWalkM: 80,
  landmarkStayM: 100,
};

export type LineKind = "before" | "during" | "count" | "landmark" | "bell" | "off";

export interface GuideLine {
  text: string;
  kind: LineKind;
  /** For a landmark line: which of step.landmarks it names. */
  landmark: number | null;
}

export interface GuidePlan {
  /** Changes with the step and the route version: the guide starts again. */
  key: string;
  mode: "bus" | "walk";
  lines: GuideLine[];
  /** The ride's stops with their positions (boarding stop first), only when every stop has one. */
  stops: { name: string | null; at: LatLng }[] | null;
  /** Metres from the boarding stop to each stop, along the line through the stops. */
  along: number[] | null;
  /** Where each of step.landmarks is seen from (null: no position, always shown). */
  landmarkAt: (LatLng | null)[];
  /** Has lines for before getting on (a bus step that starts at the stop). */
  hasBefore: boolean;
}

export interface GuideMemory {
  key: string;
  /** Furthest point reached along the ride, in metres (bus steps with stops). */
  progress: number | null;
  /** Time of the last fix used. */
  at: number | null;
  started: boolean;
  near: number[];
}

export interface GuidePrompt {
  key: string;
  /** Shown big. */
  title: string;
  /** Read aloud. */
  say: string;
}

export interface GuideView {
  /** false: show the whole step, as before. */
  on: boolean;
  started: boolean;
  stopsLeft: number | null;
  prompt: GuidePrompt | null;
  lines: GuideLine[];
  /** How many lines are left out for now ("Show all"). */
  hidden: number;
  /** Per step.landmarks: show its photo and caption now? */
  landmarkShown: boolean[];
  /** The landmarks the traveller is near now (step.landmarks indexes). */
  near: number[];
  progressM: number | null;
}

export const OFF_VIEW: GuideView = {
  on: false, started: false, stopsLeft: null, prompt: null, lines: [], hidden: 0, landmarkShown: [], near: [], progressM: null,
};

const BELL = /\b(press|ring|push)\b[^.]*\bbell\b/i;
const OFF = /\bget off\b|\btap out\b/i;
const COUNT = /\bcount\b[^.]*\bstops?\b|\bit is the \w+ stop\b|\b\d+ stops?\b/i;
const ON = /\btap your card\b(?![^.]*\bget off\b)|\bget on\b/i;

/** How the guide will follow this step, or null if it can't (the step is then shown whole). */
export function planGuide(step: RoutePackStep | undefined, packVersion: string): GuidePlan | null {
  if (!step || (step.mode !== "bus" && step.mode !== "walk")) return null;
  const mode = step.mode;
  const landmarks = step.landmarks ?? [];
  const landmarkAt = landmarks.map((m): LatLng | null =>
    m.seenFrom ? [m.seenFrom[0], m.seenFrom[1]] : typeof m.lat === "number" && typeof m.lng === "number" ? [m.lat, m.lng] : null);
  const stops = mode === "bus" && step.stops && step.stops.length >= 2 && step.stops.every((s) => s.lat !== null && s.lng !== null)
    ? step.stops.map((s) => ({ name: s.name, at: [s.lat as number, s.lng as number] as LatLng }))
    : null;

  const points = easyReadPoints(step.easyRead);
  // The special lines first: the bell, getting off, counting (bus), and lines naming a landmark with a position
  const kinds: (LineKind | null)[] = points.map((p) => {
    if (mode !== "bus") return null;
    if (BELL.test(p)) return "bell";
    if (OFF.test(p)) return "off";
    if (COUNT.test(p)) return "count";
    return null;
  });
  const named = points.map((p) => {
    const low = p.toLowerCase();
    const i = landmarks.findIndex((m, j) => landmarkAt[j] && low.includes(m.name.toLowerCase()));
    return i >= 0 ? i : null;
  });
  // Getting on ends at the last "Tap your card (to get on)" before the ride's own lines
  const firstRide = kinds.findIndex((k) => k !== null);
  const rideFrom = firstRide < 0 ? points.length : firstRide;
  let boardEnd = -1;
  if (mode === "bus") for (let i = 0; i < rideFrom; i++) if (ON.test(points[i])) boardEnd = i;

  const lines: GuideLine[] = points.map((text, i) => {
    const kind = kinds[i];
    if (kind && stops) return { text, kind, landmark: null };
    if (named[i] !== null) return { text, kind: "landmark", landmark: named[i] };
    return { text, kind: stops && i <= boardEnd ? "before" : "during", landmark: null };
  });
  // Nothing it can follow: no stops to count and no landmark to wait for
  if (!stops && !lines.some((l) => l.kind === "landmark")) return null;
  return {
    key: `${packVersion}#${step.stepIndex}`,
    mode, lines, stops, landmarkAt,
    along: stops ? cumulative(stops.map((s) => s.at)) : null,
    hasBefore: lines.some((l) => l.kind === "before"),
  };
}

export function freshMemory(plan: GuidePlan | null): GuideMemory {
  return { key: plan?.key ?? "", progress: null, at: null, started: !plan?.hasBefore, near: [] };
}

/** Takes in a GPS fix. Returns the same memory if the fix tells nothing new (or can't be trusted). */
export function advance(plan: GuidePlan | null, mem: GuideMemory, fix: Fix | null): GuideMemory {
  if (!plan || !fix) return mem;
  if (mem.at !== null && fix.at <= mem.at) return mem;
  if (fix.accuracyM !== null && fix.accuracyM > GUIDE.maxAccuracyM) return mem;
  const here: LatLng = [fix.lat, fix.lng];

  let progress = mem.progress;
  if (plan.stops && plan.along) {
    const p = project(plan, here, mem.progress);
    if (p !== null) {
      if (mem.progress !== null && mem.at !== null) {
        const seconds = Math.max(1, (fix.at - mem.at) / 1000);
        if (p - mem.progress > GUIDE.maxSpeedMps * seconds + 100) return mem;   // a jump: wait for the next fix
      }
      progress = Math.max(mem.progress ?? p, p);       // a bus doesn't go back; small GPS wobble is ignored
    }
  }
  const started = mem.started || (progress !== null && progress >= GUIDE.rideStartM);
  const show = plan.mode === "bus" ? GUIDE.landmarkBusM : GUIDE.landmarkWalkM;
  const near = plan.landmarkAt.flatMap((at, i) => {
    if (!at) return [];
    const d = metres(here, at);
    return d <= show || (mem.near.includes(i) && d <= show + GUIDE.landmarkStayM) ? [i] : [];
  });
  return { key: mem.key, progress, at: fix.at, started, near };
}

/** What to show now. `now` decides whether the last fix is recent enough to go by. */
export function guideView(plan: GuidePlan | null, mem: GuideMemory, now: number): GuideView {
  if (!plan || mem.key !== plan.key || mem.at === null || now - mem.at > GUIDE.freshMs) return OFF_VIEW;
  const { stops, along } = plan;
  let stopsLeft: number | null = null;
  let prompt: GuidePrompt | null = null;
  if (stops && along) {
    const last = stops.length - 1;
    const p = mem.progress;
    const passed = p === null ? 0 : along.slice(1, last).filter((a) => p >= a + GUIDE.passedM).length;
    stopsLeft = p !== null && p >= along[last] - GUIDE.arriveM ? 0 : last - passed;
    if (mem.started) prompt = promptFor(stopsLeft, stops[last].name);
  }
  const left = stopsLeft ?? Infinity;
  const shownLine = (l: GuideLine) => {
    if (left === 0) return l.kind === "off";              // at their stop: only how to get off ("Stay on the bus" is wrong now)
    switch (l.kind) {
      case "landmark": return l.landmark !== null && mem.near.includes(l.landmark);
      case "before": return !mem.started;
      case "during": return mem.started || !plan.hasBefore;
      case "count": return false;                         // the countdown says it
      case "bell": return mem.started && left <= 2;
      case "off": return mem.started && left <= 1;
    }
  };
  const lines = plan.lines.filter(shownLine);
  if (!lines.length && !prompt) return OFF_VIEW;          // nothing to show: better everything than nothing
  return {
    on: true, started: mem.started, stopsLeft, prompt, lines,
    hidden: plan.lines.length - lines.length,
    landmarkShown: plan.landmarkAt.map((at, i) => !at || mem.near.includes(i)),
    near: mem.near,
    progressM: mem.progress === null ? null : Math.round(mem.progress),
  };
}

function promptFor(left: number, endName: string | null): GuidePrompt {
  if (left >= 2) return { key: `left-${left}`, title: `${left} more stops`, say: `Stay on the bus. ${left} more stops.` };
  if (left === 1) {
    return { key: "bell", title: "Press the bell now",
      say: `Press the bell now. Get off at the next stop${endName ? `, ${endName}` : ""}.` };
  }
  return { key: "here", title: "Get off here", say: `This is your stop${endName ? `, ${endName}` : ""}. Get off here.` };
}

/** What to read aloud for a change from `before` to `now`, given what was already said in this step. */
export function announcements(seen: Set<string>, view: GuideView, plan: GuidePlan | null): string[] {
  if (!view.on || !plan) return [];
  const say: string[] = [];
  if (view.prompt && !seen.has(view.prompt.key)) {
    seen.add(view.prompt.key);
    say.push(view.prompt.say);
  }
  view.lines.forEach((l) => {
    if (l.kind !== "landmark" || l.landmark === null || seen.has(`landmark-${l.landmark}`)) return;
    seen.add(`landmark-${l.landmark}`);
    say.push(/[.!?]$/.test(l.text) ? l.text : `${l.text}.`);
  });
  return say;
}

/** Everything the view shows now, as keys, so a reopened trip doesn't read out what is already on screen. */
export function shownKeys(view: GuideView): string[] {
  return [
    ...(view.prompt ? [view.prompt.key] : []),
    ...view.lines.flatMap((l) => (l.kind === "landmark" && l.landmark !== null ? [`landmark-${l.landmark}`] : [])),
  ];
}

// ----- geometry: short distances in Singapore, so a flat approximation is plenty -----

const M_PER_DEG_LAT = 110_574;
const mPerDegLng = (lat: number) => 111_320 * Math.cos((lat * Math.PI) / 180);

export function metres(a: LatLng, b: LatLng): number {
  const dy = (b[0] - a[0]) * M_PER_DEG_LAT;
  const dx = (b[1] - a[1]) * mPerDegLng((a[0] + b[0]) / 2);
  return Math.hypot(dx, dy);
}

function cumulative(points: LatLng[]): number[] {
  const out = [0];
  for (let i = 1; i < points.length; i++) out.push(out[i - 1] + metres(points[i - 1], points[i]));
  return out;
}

/**
 * Where `here` is along the line through the stops, in metres from the boarding stop; null if it is too far from
 * the line to be this ride. The nearest part of the line wins, but never one well behind the progress already
 * made, so a road that passes close to itself (a loop) isn't mistaken for going back.
 */
function project(plan: GuidePlan, here: LatLng, progress: number | null): number | null {
  const pts = plan.stops!.map((s) => s.at);
  const along = plan.along!;
  let best: { d: number; a: number } | null = null;
  for (let i = 0; i < pts.length - 1; i++) {
    if (progress !== null && along[i + 1] < progress - 100) continue;
    const [a, b] = [pts[i], pts[i + 1]];
    const k = mPerDegLng(a[0]);
    const bx = (b[1] - a[1]) * k, by = (b[0] - a[0]) * M_PER_DEG_LAT;
    const hx = (here[1] - a[1]) * k, hy = (here[0] - a[0]) * M_PER_DEG_LAT;
    const len2 = bx * bx + by * by;
    const t = len2 === 0 ? 0 : Math.max(0, Math.min(1, (hx * bx + hy * by) / len2));
    const d = Math.hypot(hx - t * bx, hy - t * by);
    if (!best || d < best.d) best = { d, a: along[i] + t * Math.sqrt(len2) };
  }
  return best && best.d <= GUIDE.corridorM ? best.a : null;
}
