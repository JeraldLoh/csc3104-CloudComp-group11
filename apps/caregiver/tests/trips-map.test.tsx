import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Trip } from "../src/journey/types";
import { Trips } from "../src/planner/Trips";
import type { TripsState } from "../src/planner/useTrips";

const active = (tripId: string) =>
  ({ tripId, routeId: "R-1", travellerId: "U-1", status: "active", date: "2026-11-19", startedAt: "2026-11-19T01:38:00Z" }) as Trip;

function state(trips: Trip[], followedId: string): TripsState {
  return {
    traveller: "U-1", trips, loading: false, status: { text: "", error: false }, busyId: null, creating: false, pack: null,
    followed: trips.find((t) => t.tripId === followedId) ?? null, follow: vi.fn(), locations: {}, plans: {},
    refresh: vi.fn(), create: vi.fn(), start: vi.fn(), end: vi.fn(), preparePack: vi.fn(),
    sendBridge: vi.fn(), dismissBridge: vi.fn(), previewBridge: vi.fn(), applyBridge: vi.fn(), sendRoute: vi.fn(),
  } as unknown as TripsState;
}

describe("trips under way and the map", () => {
  it("has no map button: one trip under way is simply on the map", () => {
    render(<Trips trips={state([active("T-1")], "T-1")} routes={[]} routesLoaded />);
    expect(screen.queryByRole("button", { name: /on the map/i })).toBeNull();
    expect(screen.getByRole("region", { name: "Trip T-1, under way" }).getAttribute("aria-current")).toBe("true");
  });

  it("clicking another trip under way shows it on the map", () => {
    const trips = state([active("T-1"), active("T-2")], "T-1");
    render(<Trips trips={trips} routes={[]} routesLoaded />);
    expect(screen.queryByRole("button", { name: /on the map/i })).toBeNull();
    fireEvent.click(screen.getByRole("region", { name: "Trip T-2, under way" }));
    expect(trips.follow).toHaveBeenCalledWith("T-2");
  });
});
