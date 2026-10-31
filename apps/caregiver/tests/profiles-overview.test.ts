import { describe, expect, it } from "vitest";
import type { Incident } from "../src/alerts/incidents";
import { buildRows, summarise, type TravellerFacts } from "../src/profiles/overview";
import type { Trip } from "../src/journey/types";

const alex = { travellerId: "U-1", name: "Alex" };
const bo = { travellerId: "U-2", name: "Bo" };
const cy = { travellerId: "U-3", name: "Cy" };

function incident(travellerId: string, changes: Partial<Incident> = {}): Incident {
  return {
    incidentId: `${travellerId}#T-1#off_route#${changes.createdAt ?? 0}`, travellerId, problemType: "off_route",
    severity: "normal", state: "open", createdAt: "2026-10-12T08:00:00Z", ...changes,
  };
}

const trip = { tripId: "T-1", status: "active" } as Trip;
const known: TravellerFacts = { safeLocations: 1, caregivers: 1, activeTrip: null, tripsKnown: true };

describe("buildRows", () => {
  it("puts urgent first, then other alerts, then the all clear, by name within each", () => {
    const rows = buildRows([alex, bo, cy], [incident("U-2"), incident("U-3", { severity: "urgent" })], true, {});
    expect(rows.map((r) => [r.traveller.name, r.health])).toEqual([["Cy", "urgent"], ["Bo", "attention"], ["Alex", "clear"]]);
  });

  it("never says all clear before the alerts have loaded", () => {
    expect(buildRows([alex], [], false, {})[0].health).toBe("unknown");
  });

  it("still shows a known alert when a later reload fails", () => {
    expect(buildRows([alex], [incident("U-1", { severity: "urgent" })], false, {})[0].health).toBe("urgent");
  });

  it("ignores resolved alerts but still counts acknowledged ones", () => {
    const rows = buildRows([alex, bo], [incident("U-1", { state: "resolved" }), incident("U-2", { state: "acknowledged" })], true, {});
    expect(rows.map((r) => [r.traveller.name, r.health])).toEqual([["Bo", "attention"], ["Alex", "clear"]]);
  });

  it("lists a traveller's most pressing alert first", () => {
    const rows = buildRows([alex], [
      incident("U-1", { createdAt: "2026-10-12T09:00:00Z" }),
      incident("U-1", { createdAt: "2026-10-12T08:00:00Z", severity: "urgent" }),
    ], true, {});
    expect(rows[0].unresolved[0].severity).toBe("urgent");
  });
});

describe("summarise", () => {
  it("counts travellers, those needing attention, urgent alerts and running trips", () => {
    const rows = buildRows([alex, bo, cy], [
      incident("U-1", { severity: "urgent" }),
      incident("U-1", { severity: "urgent", createdAt: "2026-10-12T09:00:00Z" }),
      incident("U-2"),
    ], true, { "U-1": { ...known, activeTrip: trip }, "U-2": known, "U-3": known });
    expect(summarise(rows)).toEqual({ travellers: 3, needAttention: 2, urgentAlerts: 2, onTrip: 1 });
  });

  it("leaves alert numbers blank rather than saying zero when they aren't known", () => {
    const totals = summarise(buildRows([alex], [], false, { "U-1": known }));
    expect(totals.needAttention).toBeNull();
    expect(totals.urgentAlerts).toBeNull();
  });

  it("leaves the trip number blank if any traveller's trips couldn't be checked", () => {
    const rows = buildRows([alex, bo], [], true, { "U-1": known, "U-2": { ...known, tripsKnown: false } });
    expect(summarise(rows).onTrip).toBeNull();
  });
});
