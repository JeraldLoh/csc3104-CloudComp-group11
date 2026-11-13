// Runs the step-by-step guide (src/lib/guide.ts) on each GPS fix, and tells the traveller when something new comes
// up: a short buzz, and the new prompt read aloud ("Stay on the bus. 5 more stops.", "Look out for Oasis Terraces
// on your right.", "Press the bell now."). The phone's own voice, so it works with no signal. Reading aloud can be
// switched off on the card (remembered on this phone); the buzz and the words on screen stay.
import { useEffect, useMemo, useRef, useState } from "react";
import { advance, announcements, freshMemory, guideView, planGuide, shownKeys, type GuideMemory, type GuideView } from "../lib/guide";
import type { Fix } from "../lib/tracker";
import type { RoutePackStep } from "../types";

export interface Guide extends GuideView {
  readOut: boolean;
  setReadOut(on: boolean): void;
}

const READ_OUT_KEY = "careroute.readOut";
/** Re-check now and then, so a fix that has gone stale (no GPS) brings the whole step back. */
const RECHECK_MS = 10_000;

function savedReadOut(): boolean {
  try {
    return localStorage.getItem(READ_OUT_KEY) !== "off";
  } catch {
    return true;
  }
}

/** Says it in the phone's voice, instead of anything it was saying. */
export function sayAloud(text: string): void {
  if (typeof window === "undefined" || !("speechSynthesis" in window)) return;
  window.speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 0.9;
  window.speechSynthesis.speak(u);
}

export function useGuide(step: RoutePackStep | undefined, packVersion: string | undefined, fix: Fix | null, active: boolean): Guide {
  const plan = useMemo(() => (active && packVersion ? planGuide(step, packVersion) : null), [active, step, packVersion]);
  const [readOut, setReadOutState] = useState(savedReadOut);
  const [, recheck] = useState(0);

  // Memory of the ride so far. advance() ignores a fix it has already taken, so running it while rendering is safe.
  const mem = useRef<GuideMemory>(freshMemory(plan));
  if (mem.current.key !== (plan?.key ?? "")) mem.current = freshMemory(plan);
  mem.current = advance(plan, mem.current, fix);
  const view = guideView(plan, mem.current, Date.now());

  useEffect(() => {
    if (!plan) return;
    const t = setInterval(() => recheck((n) => n + 1), RECHECK_MS);
    return () => clearInterval(t);
  }, [plan]);

  // Tell the traveller what's new. The first view of a step only notes what's on screen (they are looking at it),
  // so reopening the app mid-ride doesn't read it all out again.
  const said = useRef({ key: "", keys: new Set<string>(), primed: false });
  const signature = `${plan?.key}|${view.on}|${view.prompt?.key}|${view.lines.map((l) => l.text).join("|")}`;
  useEffect(() => {
    if (!plan) return;
    if (said.current.key !== plan.key) said.current = { key: plan.key, keys: new Set(), primed: false };
    if (!view.on) return;
    const s = said.current;
    if (!s.primed) {
      shownKeys(view).forEach((k) => s.keys.add(k));
      s.primed = true;
      return;
    }
    const say = announcements(s.keys, view, plan);
    if (!say.length) return;
    navigator.vibrate?.([250, 120, 250]);
    if (readOut) sayAloud(say.join(" "));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `signature` is what changes in `view`
  }, [signature, plan, readOut]);

  return {
    ...view,
    readOut,
    setReadOut: (on: boolean) => {
      setReadOutState(on);
      try {
        localStorage.setItem(READ_OUT_KEY, on ? "on" : "off");
      } catch { /* not remembered: still works for this trip */ }
      if (!on && typeof window !== "undefined" && "speechSynthesis" in window) window.speechSynthesis.cancel();
    },
  };
}
