// Plain-language text for plan results. No React here, so it is easy to test.
import type { LiveEntry, PlanOption } from "../journey/types";

export const CROWD_TEXT: Record<string, string> = { low: "quiet", moderate: "getting busy", high: "very busy" };

export const minutes = (seconds: number | null | undefined) => Math.round((seconds ?? 0) / 60);

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
