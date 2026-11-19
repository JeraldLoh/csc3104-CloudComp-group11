import * as L from "leaflet";
import { describe, expect, it } from "vitest";
import { drawTraveller, locationAlert, travellerLabel, TRAVELLER_COLORS } from "../src/map/traveller";

const incident = (problemType: string, tripId: string | undefined, state: "open" | "acknowledged" | "resolved" = "open") =>
  ({ problemType, tripId, state });

describe("the traveller on a map (one helper for the dashboard and the Trips tab)", () => {
  it("finds an open alert about where they are, for this trip only", () => {
    expect(locationAlert([incident("off_route", "T-1")], "T-1")).toBe("May have gone off route");
    expect(locationAlert([incident("wrong_direction", "T-1", "acknowledged")], "T-1")).toBe("May be going the wrong way");
    expect(locationAlert([incident("off_route", "T-0")], "T-1")).toBeNull();          // an earlier trip's
    expect(locationAlert([incident("off_route", "T-1", "resolved")], "T-1")).toBeNull();
    expect(locationAlert([incident("help_requested", "T-1")], "T-1")).toBeNull();     // not about where they are
  });

  it("says the alert in the label", () => {
    expect(travellerLabel({ label: "Alex", alert: "May have gone off route" })).toBe("Alex: May have gone off route");
    expect(travellerLabel({ label: "Alex", alert: null })).toBe("Alex");
  });

  it("draws the trail and dot: amber with an alert, grey before a check-in, blue otherwise", () => {
    const dot = (t: Partial<Parameters<typeof drawTraveller>[1]>) => {
      const layer = L.layerGroup();
      drawTraveller(layer, { lat: 1.3, lng: 103.9, trail: [[1.3, 103.9], [1.31, 103.91]], label: "Alex", ...t });
      const marks = layer.getLayers();
      return { count: marks.length, fill: (marks[marks.length - 1] as L.CircleMarker).options.fillColor };
    };
    expect(dot({})).toEqual({ count: 2, fill: TRAVELLER_COLORS.here });                // the trail, then the dot
    expect(dot({ guessed: true }).fill).toBe(TRAVELLER_COLORS.guessed);
    expect(dot({ alert: "May have gone off route" }).fill).toBe(TRAVELLER_COLORS.alert);
  });
});
