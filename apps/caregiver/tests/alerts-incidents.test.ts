import { describe, expect, it } from "vitest";
import { applyEvent, mergeIncidents, sortForDisplay, type AlertEvent, type Incident } from "../src/alerts/incidents";

const base: Incident = {
  incidentId: "U-042#T-001#off_route#2",
  travellerId: "U-042",
  tripId: "T-001",
  problemType: "off_route",
  severity: "normal",
  state: "open",
  detectionCount: 1,
  createdAt: "2026-10-12T08:15:31Z",
  updatedAt: "2026-10-12T08:15:31Z",
};

function event(type: string, changes: Partial<Incident>): AlertEvent {
  return { event: `incident.${type}`, incident: { ...base, ...changes, message: "..." }, sentAt: "2026-10-12T08:16:00Z" };
}

describe("applyEvent", () => {
  it("adds a new incident", () => {
    const next = applyEvent({}, event("opened", {}));
    expect(next[base.incidentId].state).toBe("open");
  });

  it("updates an incident it already has, keeping fields the event doesn't carry", () => {
    const ack = event("acknowledged", { state: "acknowledged", updatedAt: "2026-10-12T08:16:00Z" });
    delete ack.incident.tripId; // alert-event.json has no tripId
    const next = applyEvent({ [base.incidentId]: base }, ack);
    expect(next[base.incidentId].state).toBe("acknowledged");
    expect(next[base.incidentId].tripId).toBe("T-001");
  });

  it("ignores an event older than what it has (e.g. arrived after a reload)", () => {
    const resolved = { ...base, state: "resolved" as const, updatedAt: "2026-10-12T08:20:00Z" };
    const next = applyEvent({ [base.incidentId]: resolved }, event("acknowledged", {
      state: "acknowledged", updatedAt: "2026-10-12T08:16:00Z",
    }));
    expect(next[base.incidentId].state).toBe("resolved");
  });

  it("doesn't keep the event's message text on the incident", () => {
    const next = applyEvent({}, event("opened", {}));
    expect("message" in next[base.incidentId]).toBe(false);
  });
});

describe("mergeIncidents", () => {
  it("keeps the newer version of each incident", () => {
    const newer = { ...base, detectionCount: 5, updatedAt: "2026-10-12T08:20:00Z" };
    const next = mergeIncidents({ [base.incidentId]: newer }, [base]);
    expect(next[base.incidentId].detectionCount).toBe(5);
  });
});

describe("sortForDisplay", () => {
  it("puts urgent first, then other open alerts newest first, then resolved", () => {
    const resolved = { ...base, incidentId: "r", state: "resolved" as const, createdAt: "2026-10-12T09:00:00Z" };
    const older = { ...base, incidentId: "older", createdAt: "2026-10-12T08:00:00Z" };
    const newer = { ...base, incidentId: "newer", createdAt: "2026-10-12T08:30:00Z" };
    const urgent = { ...base, incidentId: "urgent", severity: "urgent" as const, createdAt: "2026-10-12T07:00:00Z" };

    const ids = sortForDisplay([resolved, older, newer, urgent]).map((i) => i.incidentId);

    expect(ids).toEqual(["urgent", "newer", "older", "r"]);
  });
});
