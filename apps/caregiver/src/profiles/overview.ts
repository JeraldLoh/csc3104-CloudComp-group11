// What the dashboard says about each traveller, worked out from their profile, alerts and trips.
// Pure functions, no I/O, so they're easy to test (tests/profiles-overview.test.ts).
import { sortForDisplay, type Incident } from "../alerts/incidents";
import type { Trip } from "../journey/types";
import type { LiveTrip, Traveller } from "./api";

/** "unknown": their alerts haven't loaded, so nobody may be told all is well. */
export type Health = "urgent" | "attention" | "unknown" | "clear";

/** What was fetched for one traveller. null / `tripsKnown: false` = that service didn't answer. */
export interface TravellerFacts {
  safeLocations: number | null;
  caregivers: number | null;
  activeTrip: Trip | null;
  tripsKnown: boolean;
}

export interface TravellerRow {
  traveller: Traveller;
  health: Health;
  /** Open and acknowledged alerts, most pressing first. */
  unresolved: Incident[];
  facts: TravellerFacts | null;
  /** Their latest trip from the phone's check-ins, if any. */
  live: LiveTrip | null;
}

const ORDER: Health[] = ["urgent", "attention", "unknown", "clear"];

function healthOf(unresolved: Incident[], alertsKnown: boolean): Health {
  if (unresolved.some((i) => i.severity === "urgent")) return "urgent";
  if (unresolved.length > 0) return "attention";
  return alertsKnown ? "clear" : "unknown";
}

/** One row per traveller, those who need attention first (by name within each group: the list comes sorted). */
export function buildRows(travellers: Traveller[], incidents: Incident[], alertsKnown: boolean,
                          facts: Record<string, TravellerFacts>, live: Record<string, LiveTrip> = {}): TravellerRow[] {
  const rows = travellers.map((traveller) => {
    const unresolved = sortForDisplay(
      incidents.filter((i) => i.travellerId === traveller.travellerId && i.state !== "resolved"));
    return { traveller, unresolved, health: healthOf(unresolved, alertsKnown),
             facts: facts[traveller.travellerId] ?? null, live: live[traveller.travellerId] ?? null };
  });
  return rows.sort((a, b) => ORDER.indexOf(a.health) - ORDER.indexOf(b.health));
}

/** The numbers across the top. null = can't honestly say yet. */
export function summarise(rows: TravellerRow[], liveKnown = false) {
  const needAttention = rows.filter((r) => r.health === "urgent" || r.health === "attention").length;
  const anyUnknown = rows.some((r) => r.health === "unknown");
  const tripsKnown = liveKnown || rows.every((r) => r.facts?.tripsKnown);
  return {
    travellers: rows.length,
    needAttention: anyUnknown ? null : needAttention,
    urgentAlerts: anyUnknown ? null
      : rows.flatMap((r) => r.unresolved).filter((i) => i.severity === "urgent").length,
    onTrip: tripsKnown ? rows.filter((r) => r.live ? !r.live.arrivedAt : r.facts?.activeTrip).length : null,
  };
}

/** How long ago, in words, for "seen 2 min ago". */
export function seenAgo(iso: string, now = Date.now()): string {
  const minutes = Math.floor((now - new Date(iso).getTime()) / 60_000);
  if (minutes < 1) return "just now";
  return minutes < 60 ? `${minutes} min ago` : `${Math.floor(minutes / 60)} h ago`;
}
