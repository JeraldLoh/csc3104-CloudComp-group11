import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Landmark, RouteStep, SavedRoute } from "../src/journey/types";
import { SavedRoutes } from "../src/planner/SavedRoutes";
import type { SavedRoutesState } from "../src/planner/useSavedRoutes";

const stop = (name: string, code: string | null = null) => ({ name, code, lat: 1.3, lng: 103.9 });

function step(stepIndex: number, mode: RouteStep["mode"], easyRead: string, extra: Partial<RouteStep> = {}): RouteStep {
  return { stepIndex, mode, instruction: `Step ${stepIndex + 1}`, easyRead, from: stop("A"), to: stop("B"), ...extra };
}

function route(routeId: string, steps: RouteStep[]): SavedRoute {
  return {
    routeId, version: 1, approved: false, name: routeId, travellerId: "U-1", caregiverId: "G-1",
    origin: { name: "From", lat: 1.3, lng: 103.9 }, destination: { name: "To", lat: 1.31, lng: 103.91 }, steps, path: [],
  } as SavedRoute;
}

function state(opened: SavedRoute | null, routes: SavedRoute[]): SavedRoutesState {
  return {
    traveller: "U-1", routes, loaded: true, loading: false, status: { text: "", error: false }, opened,
    approvingId: null, deletingId: null, saving: false, outOfDate: false, suggestingId: null, suggestion: null,
    maxEasyRead: 300, refresh: vi.fn(), open: vi.fn(), close: vi.fn(), approve: vi.fn(), update: vi.fn(),
    remove: vi.fn(), suggest: vi.fn(), uploadPhoto: vi.fn(),
  } as unknown as SavedRoutesState;
}

const preview = () => screen.getByRole("complementary", { name: /on the traveller's phone/ });

describe("saved routes", () => {
  it("opening a shorter route while editing another doesn't crash the page", () => {
    const long = route("R-LONG", [step(0, "walk", "Walk to the stop."), step(1, "bus", "Take bus 28."), step(2, "walk", "You have arrived.")]);
    const short = route("R-SHORT", [step(0, "walk", "Walk to Bedok.")]);
    const { rerender } = render(<SavedRoutes saved={state(long, [long, short])} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByText("Step 3"));              // the preview shows the third step

    rerender(<SavedRoutes saved={state(short, [long, short])} />);

    expect(screen.getByRole("heading", { name: "R-SHORT" })).toBeTruthy();
    expect(screen.getByText("Walk to Bedok")).toBeTruthy();
  });

  it("the phone preview shows the wording as points and the landmarks, as the traveller app does", () => {
    const mall: Landmark = { name: "City Plaza", lat: 1.31, lng: 103.89, photoUrl: "http://s3/city-plaza.jpg" };
    const station: Landmark = { name: "Paya Lebar MRT Station", lat: 1.31, lng: 103.89, sameView: "step" };
    const bus = step(0, "bus", "Raise your hand for bus 28. After Block 14, look out for City Plaza. Look for Paya Lebar MRT Station.",
      { photoUrl: "http://s3/stop.jpg", landmarks: [mall, station] });
    const r = route("R-PL", [bus]);
    render(<SavedRoutes saved={state(r, [r])} />);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));

    const phone = within(preview());
    expect(phone.getAllByRole("listitem").map((li) => li.textContent)).toEqual(expect.arrayContaining([
      "Raise your hand for bus 28", "After Block 14, look out for City Plaza", "Look for Paya Lebar MRT Station",
    ]));
    expect(phone.getByRole("img", { name: "City Plaza" })).toBeTruthy();
    expect(phone.queryByText("Look for City Plaza")).toBeNull();     // the points already say it
    expect(phone.getByText("Look for Paya Lebar MRT Station: it is in the step photo above.")).toBeTruthy();
  });
});
