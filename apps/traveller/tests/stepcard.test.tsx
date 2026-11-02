import { render, screen, waitFor } from "@testing-library/react";
import { StepCard } from "../src/components/StepCard";
import { validatePack } from "../src/lib/offline";
import { DEMO_PACK } from "../src/lib/demo";
import type { LoadedPack } from "../src/lib/pack";
// A pack made by the Journey Service's own build_pack (tests/fixtures/journey_pack.py), not typed by hand.
import journeyPack from "./fixtures/journey-pack.json";

const fromJourneyService: LoadedPack = {
  pack: validatePack(structuredClone(journeyPack)),
  baseUrl: "https://careroute-files.s3.ap-southeast-1.amazonaws.com/packs/R-DE2001/",
  source: "network",
};
const demo: LoadedPack = { pack: DEMO_PACK, baseUrl: "http://localhost/", source: "demo" };

const photos = (container: HTMLElement) => [...container.querySelectorAll("img")].map((i) => i.getAttribute("src") ?? "");

describe("StepCard", () => {
  it("shows a bus step from the Journey Service: one action per line, the step photo, then each landmark", async () => {
    const step = fromJourneyService.pack.steps[1];
    const { container } = render(<StepCard step={step} loaded={fromJourneyService} index={1} total={2} tripId="T-1" />);

    const points = screen.getAllByRole("listitem").map((li) => li.textContent);
    expect(points.slice(0, 6)).toEqual([
      "Wait for bus number 190", "Tap your card", "Count 6 stops",
      "Look out for Oasis Terraces on your right", "Press the bell", "Get off at Block 221",
    ]);

    // Step photo first, then the landmark that has its own photo (Block 221 doesn't: it's in the step photo).
    await waitFor(() => expect(photos(container)).toHaveLength(2));
    expect(photos(container)[0]).toContain("1.39350_103.89620.jpg");
    expect(photos(container)[1]).toContain("1.40260_103.91280_from_");
    expect(screen.getByText("Look for Oasis Terraces")).toBeTruthy();
    expect(screen.getByText("Look for Block 221: it is in the photo above.")).toBeTruthy();
  });

  it("a landmark seen in the step photo says so (sameView, when the pack sends it)", async () => {
    const step = DEMO_PACK.steps[2];
    const { container } = render(<StepCard step={step} loaded={demo} index={2} total={4} tripId="T-1" />);
    await waitFor(() => expect(photos(container)).toHaveLength(2));
    expect(photos(container)[1]).toContain("demo-media/waterway-point.svg");
    expect(screen.getByText("Look for Waterway Point")).toBeTruthy();
    expect(screen.getByText("Look for Block 221: it is in the step photo above.")).toBeTruthy();
  });

  it("a one-sentence step stays a sentence, with no landmark list", () => {
    render(<StepCard step={DEMO_PACK.steps[0]} loaded={demo} index={0} total={4} tripId="T-1" />);
    expect(document.getElementById("step-text")?.tagName).toBe("P");
    expect(screen.getByText("Walk to the bus stop near the playground.")).toBeTruthy();
    expect(screen.queryByRole("list", { name: "Things to look out for" })).toBeNull();
  });

  it("only landmarks without their own photo: no empty photo list, just the line saying where to look", () => {
    const step = { ...DEMO_PACK.steps[2], landmarks: [{ name: "Block 221", photoUrl: null, sameView: "step" }] };
    render(<StepCard step={step} loaded={demo} index={2} total={4} tripId="T-1" />);
    expect(screen.queryByRole("list", { name: "Things to look out for" })).toBeNull();
    expect(screen.getByText("Look for Block 221: it is in the step photo above.")).toBeTruthy();
  });
});
