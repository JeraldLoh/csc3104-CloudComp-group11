// Plain-language text for plan results. No React here, so it is easy to test.
import type { LiveEntry, PlanOption, RouteStep } from "../journey/types";

export const CROWD_TEXT: Record<string, string> = { low: "quiet", moderate: "getting busy", high: "very busy" };

export const minutes = (seconds: number | null | undefined) => Math.round((seconds ?? 0) / 60);

// Short forms that end in a full stop inside a name ("St. Andrew's"), so they don't end a point.
const NOT_AN_END = /\b(?:St|Mt|Dr|Rd|Ave|No|Jln|Blk|Opp)\.$/i;

/** Easy-read wording as bullet points: one per sentence, without the full stop.
 *  "Wait for bus number 190. Tap your card." -> ["Wait for bus number 190", "Tap your card"] */
export function easyReadPoints(text: string | null | undefined): string[] {
  const points: string[] = [];
  for (const part of (text ?? "").split(/(?<=[.!?])\s+/)) {
    if (points.length && NOT_AN_END.test(points[points.length - 1])) points[points.length - 1] += ` ${part}`;
    else points.push(part);
  }
  return points.map((p) => p.trim().replace(/\.$/, "")).filter(Boolean);
}

/** What photo the caregiver could add for a step that has none. Street View can't see inside stations or malls,
 *  and has no recent picture of some stops, so these are the steps that need the caregiver's own photo. */
export function photoHint(step: Pick<RouteStep, "mode" | "to"> & Partial<Pick<RouteStep, "from">>): string {
  const isStation = (code: string | null | undefined) => !!code && !/^\d+$/.test(code);   // NE16; bus stops are digits
  if (step.mode === "train") return "Add a photo of the gantry or the platform, so the traveller knows where to go.";
  if (step.mode === "walk" && isStation(step.from?.code) && isStation(step.to?.code))      // a change of line
    return "Add a photo of the signs to the other line, so the traveller knows which way to walk.";
  if (step.mode === "walk" && isStation(step.to?.code))
    return "No street photo of this station. Add one of the entrance or the gantry.";
  if (step.mode === "bus") return "No street photo of this stop. Add one of the bus stop.";
  return "No street photo of this place. Add one of where this walk ends.";
}

/** A Street View photo the service took only because nothing recent was there (stored under rough/): it may be
 *  years old, or inside a shop, so the caregiver should check it. */
export const isRoughPhoto = (photoKey: string | null | undefined) =>
  photoKey?.startsWith("media/streetview/v2/rough/") ?? false;

/** Under a rough photo: what to check, and what to add instead. */
export function roughPhotoHint(step: Pick<RouteStep, "mode" | "to">): string {
  const code = step.to?.code ?? "";
  const better = step.mode === "walk" && code && !/^\d+$/.test(code) ? "the entrance or the gantry"
    : step.mode === "bus" ? "the bus stop" : "where this walk ends";
  return `Only an old street photo was found here: check it shows the right place, or add one of ${better}.`;
}

export const BULLET = "• ";

/** Easy-read wording as the editor shows it: one "• point" per line. */
export const toBulletLines = (text: string | null | undefined) =>
  easyReadPoints(text).map((p) => BULLET + p).join("\n");

/** The editor's lines back to the stored wording: one sentence per line, bullets and blank lines dropped.
 *  "• Get on the bus\n• Tap your card" -> "Get on the bus. Tap your card." */
/** The landmarks the wording names: one the caregiver edits out of the text goes (the service drops it on save too). */
export function namedLandmarks<T extends { name: string }>(landmarks: T[] | null | undefined, text: string | null | undefined): T[] {
  const words = (text ?? "").toLowerCase();
  return (landmarks ?? []).filter((m) => words.includes(m.name.toLowerCase()));
}

/** Where a landmark without a photo of its own can be seen: its photo would have looked the same as that one. */
export function landmarkSeenIn(m: { name: string; sameView?: string | null }): string {
  return m.sameView === "step"
    ? `Look for ${m.name}: it is in the step photo above.`
    : `Look for ${m.name}: it is in the photo of ${m.sameView}.`;
}

export function fromBulletLines(lines: string): string {
  return lines.split("\n")
    .map((line) => line.replace(/^\s*[•*-]\s*/, "").trim())
    .filter(Boolean)
    .map((line) => (/[.!?]$/.test(line) ? line : `${line}.`))
    .join(" ");
}

/** "Option 1: 32 min, bus + train, walk 250 m, quiet, $1.80 (2 notices)" */
export function optionLabel(o: PlanOption): string {
  const s = o.summary;
  const parts = [
    `${minutes(s.durationS)} min`,
    s.modes.join(" + "),
    `walk ${s.walkDistanceM} m`,
  ];
  if (s.crowd) parts.push(CROWD_TEXT[s.crowd] ?? s.crowd);
  if (s.fare != null) parts.push(`$${s.fare.toFixed(2)}`);
  const notices = o.warnings.length
    ? ` (${o.warnings.length} notice${o.warnings.length > 1 ? "s" : ""})`
    : "";
  return `Option ${o.option}: ${parts.join(", ")}${notices}`;
}

/** What is known right now about a step: platform crowding, or the next buses. */
export function liveText(l: LiveEntry): string {
  if (l.station) return `Platform at ${l.station}: ${(l.crowd && CROWD_TEXT[l.crowd]) || "unknown"} (${l.source ?? "no source"})`;
  const buses = l.nextBuses ?? [];
  if (!buses.length) return `No live arrival data for bus ${l.serviceNo ?? ""}`.trim();
  const times = buses.map((b) => b.display + (b.load ? ` (${b.load})` : "")).join(", ");
  const crowd = l.crowd ? `. The next bus is ${CROWD_TEXT[l.crowd] ?? l.crowd}.` : "";
  return `Next bus ${l.serviceNo} at stop ${l.busStopCode}: ${times}${crowd}`;
}
