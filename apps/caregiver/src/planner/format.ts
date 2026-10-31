// Takes output from easy_read.py and formats it for the caregiver's planner. The service is in apps/journey-service.
// Splits the wording into bullet points, and adds hints about photos and landmarks. The caregiver can edit it before saving it to the route.
import type { LiveEntry, PlanOption, RouteStep } from "../journey/types";

export const CROWD_TEXT: Record<string, string> = { low: "quiet", moderate: "getting busy", high: "very busy" };

export const minutes = (seconds: number | null | undefined) => Math.round((seconds ?? 0) / 60);

const NOT_AN_END = /\b(?:St|Mt|Dr|Rd|Ave|No|Jln|Blk|Opp)\.$/i;

export function easyReadPoints(text: string | null | undefined): string[] {
  const points: string[] = [];
  for (const part of (text ?? "").split(/(?<=[.!?])\s+/)) {
    if (points.length && NOT_AN_END.test(points[points.length - 1])) points[points.length - 1] += ` ${part}`;
    else points.push(part);
  }
  return points.map((p) => p.trim().replace(/\.$/, "")).filter(Boolean);
}

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

export const isRoughPhoto = (photoKey: string | null | undefined) =>
  photoKey?.startsWith("media/streetview/v2/rough/") ?? false;

export function roughPhotoHint(step: Pick<RouteStep, "mode" | "to">): string {
  const code = step.to?.code ?? "";
  const better = step.mode === "walk" && code && !/^\d+$/.test(code) ? "the entrance or the gantry"
    : step.mode === "bus" ? "the bus stop" : "where this walk ends";
  return `Only an old street photo was found here: check it shows the right place, or add one of ${better}.`;
}

export const BULLET = "• ";

export const toBulletLines = (text: string | null | undefined) =>
  easyReadPoints(text).map((p) => BULLET + p).join("\n");

export function namedLandmarks<T extends { name: string }>(landmarks: T[] | null | undefined, text: string | null | undefined): T[] {
  const words = (text ?? "").toLowerCase();
  return (landmarks ?? []).filter((m) => words.includes(m.name.toLowerCase()));
}

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

export function liveText(l: LiveEntry): string {
  if (l.station) return `Platform at ${l.station}: ${(l.crowd && CROWD_TEXT[l.crowd]) || "unknown"} (${l.source ?? "no source"})`;
  const buses = l.nextBuses ?? [];
  if (!buses.length) return `No live arrival data for bus ${l.serviceNo ?? ""}`.trim();
  const times = buses.map((b) => b.display + (b.load ? ` (${b.load})` : "")).join(", ");
  const crowd = l.crowd ? `. The next bus is ${CROWD_TEXT[l.crowd] ?? l.crowd}.` : "";
  return `Next bus ${l.serviceNo} at stop ${l.busStopCode}: ${times}${crowd}`;
}
