import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Trip } from "../src/journey/types";
import { DashboardPage } from "../src/profiles/DashboardPage";
import type { MyTravellers } from "../src/profiles/useMyTravellers";

class FakeEventSource {
  static last: FakeEventSource;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((msg: { data: string }) => void) | null = null;
  constructor(readonly url: string) {
    FakeEventSource.last = this;
  }
  addEventListener() {}
  close() {}
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

// What the Alert Service answers; a test changes this before rendering.
let answers: Record<string, () => Response>;

function my(travellers: MyTravellers["travellers"]): MyTravellers {
  return { travellers, loaded: true, error: null, reload: async () => {} };
}

const alex = { travellerId: "U-1", name: "Alex" };
const journey = { listTrips: vi.fn<(id: string) => Promise<Trip[]>>() };

function show(travellers = [alex], onOpenProfile = vi.fn()) {
  // The same `my` every render, as App gives it (a new `reload` would mean a new load).
  render(<DashboardPage caregiverId="G-1" my={my(travellers)} journey={journey} onOpenProfile={onOpenProfile} />);
  return onOpenProfile;
}

const offRoute = { incidentId: "U-1#T-1#off_route#2", travellerId: "U-1", problemType: "off_route", severity: "normal",
                   state: "open", createdAt: "2026-10-12T08:00:00Z", updatedAt: "2026-10-12T08:00:00Z" };

beforeEach(() => {
  answers = {
    "/api/incidents": () => json([]),
    "/safe-locations": () => json([{ locationId: "L-1", name: "Home" }, { locationId: "L-2", name: "School" }]),
    "/caregivers": () => json(["G-1"]),
  };
  journey.listTrips.mockReset().mockResolvedValue([]);
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const paths = Object.keys(answers).sort((a, b) => b.length - a.length); // the most specific path wins
    const match = paths.find((path) => url.includes(path));
    return match ? answers[match]() : json({}, 404);
  }));
});

afterEach(() => vi.unstubAllGlobals());

describe("DashboardPage", () => {
  it("shows a traveller with nothing wrong as all clear, with their profile numbers", async () => {
    show();
    expect(await screen.findByText("All clear")).toBeTruthy();
    expect(await screen.findByText("2 places")).toBeTruthy();
    expect(screen.getByText("1 caregiver")).toBeTruthy();
    expect(screen.getByText("No trip of yours under way")).toBeTruthy();
  });

  it("does not say all clear when the alerts can't be loaded", async () => {
    answers["/api/incidents"] = () => json({ error: { message: "Service down" } }, 503);
    show();
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(await screen.findByText("Can't check alerts")).toBeTruthy();
    expect(screen.queryByText("All clear")).toBeNull();
  });

  it("still shows alerts and profile numbers when the Journey Service is down", async () => {
    journey.listTrips.mockRejectedValue(new Error("down"));
    show();
    expect(await screen.findByText("Can't check trips right now")).toBeTruthy();
    expect(screen.getByText("All clear")).toBeTruthy();
    expect(screen.getByText("2 places")).toBeTruthy();
  });

  it("shows a trip that is under way", async () => {
    journey.listTrips.mockResolvedValue([
      { tripId: "T-OLD", status: "completed" } as Trip,
      { tripId: "T-NOW", status: "active" } as Trip,
    ]);
    show();
    expect(await screen.findByText("Trip T-NOW under way")).toBeTruthy();
  });

  it("updates live when an alert arrives, and moves that traveller to the top", async () => {
    show([alex, { travellerId: "U-2", name: "Bo" }]);
    await waitFor(() => expect(screen.getAllByText("All clear")).toHaveLength(2));

    act(() => FakeEventSource.last.onmessage?.({ data: JSON.stringify({
      event: "incident.opened", sentAt: "2026-10-12T08:00:00Z",
      incident: { incidentId: "x", travellerId: "U-2", problemType: "no_response", severity: "urgent",
                  state: "open", createdAt: "2026-10-12T08:00:00Z" },
    }) }));

    const cards = await screen.findAllByRole("listitem");
    expect(within(cards[0]).getByText("Bo")).toBeTruthy();
    expect(within(cards[0]).getByText("Urgent")).toBeTruthy();
    expect(within(cards[0]).getByText("Phone has stopped responding")).toBeTruthy();
  });

  it("opens a traveller to act on their alerts, and goes back to everyone", async () => {
    answers["/api/incidents/"] = () => json({ ...offRoute, state: "acknowledged", acknowledgedBy: "G-1", updatedAt: "2026-10-12T08:05:00Z" });
    answers["/api/incidents"] = () => json([offRoute]);
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Open (1 alert)" }));

    fireEvent.click(await screen.findByRole("button", { name: "Acknowledge" }));
    expect(await screen.findByText(/acknowledged by G-1/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Acknowledge" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /All travellers/ }));
    expect(await screen.findByRole("button", { name: /Open/ })).toBeTruthy();
  });

  it("only lists the opened traveller's alerts", async () => {
    answers["/api/incidents"] = () => json([offRoute]);
    show([alex, { travellerId: "U-2", name: "Bo" }]);
    await screen.findByRole("button", { name: "Open (1 alert)" });
    fireEvent.click(await screen.findByRole("button", { name: /Bo/ }));
    expect(await screen.findByRole("heading", { name: "Bo" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Acknowledge" })).toBeNull();
  });

  it("doesn't say all clear inside a traveller when their alerts can't be loaded", async () => {
    answers["/api/incidents"] = () => json({ error: { message: "Service down" } }, 503);
    show();
    await screen.findByText("Can't check alerts");
    fireEvent.click(screen.getByRole("button", { name: /Alex/ }));
    expect(await screen.findByText("Alerts not known yet")).toBeTruthy();
    expect(screen.queryByText("All clear")).toBeNull();
  });

  it("leads to the traveller's profile", async () => {
    const onOpenProfile = show();
    fireEvent.click(await screen.findByRole("button", { name: "Open" }));
    fireEvent.click(await screen.findByRole("button", { name: "Edit profile" }));
    expect(onOpenProfile).toHaveBeenCalledWith("U-1");
  });

  it("points a new caregiver at adding a traveller", async () => {
    const onOpenProfile = show([]);
    fireEvent.click(await screen.findByRole("button", { name: "Add a traveller" }));
    expect(onOpenProfile).toHaveBeenCalledWith();
  });
});
