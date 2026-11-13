import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StepCard } from "../src/components/StepCard";
import { validatePack } from "../src/lib/offline";
import { DEMO_PACK } from "../src/lib/demo";
import type { LoadedPack } from "../src/lib/pack";
import { advance, freshMemory, guideView, planGuide } from "../src/lib/guide";
import type { Guide } from "../src/app/useGuide";
import type { Fix } from "../src/lib/tracker";
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
    // The pack says which photo (sameView "step"), so the line names it
    expect(screen.getByText("Look for Block 221: it is in the step photo above.")).toBeTruthy();
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

  it("Listen reads the step and then the landmark lines (the phone's voice, no recording)", () => {
    const said: string[] = [];
    const speech = { cancel: vi.fn(), speak: vi.fn((u: { text: string }) => said.push(u.text)) };
    vi.stubGlobal("speechSynthesis", speech);
    vi.stubGlobal("SpeechSynthesisUtterance", class { rate = 1; onend = null; onerror = null; constructor(public text: string) {} });
    try {
      const step = fromJourneyService.pack.steps[1];
      render(<StepCard step={step} loaded={fromJourneyService} index={1} total={2} tripId="T-1" />);
      fireEvent.click(screen.getByRole("button", { name: /listen/i }));
      expect(said).toEqual([`${step.easyRead} Look for Oasis Terraces. Look for Block 221: it is in the step photo above.`]);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("StepCard, one thing at a time (with a guide)", () => {
  // The demo's bus ride (step 2): stops and landmark positions, so the guide can follow it
  const step = DEMO_PACK.steps[2];
  const plan = planGuide(step, DEMO_PACK.packVersion)!;
  const stops = step.stops!;
  const at = (i: number, t: number): Fix => ({ lat: stops[i].lat!, lng: stops[i].lng!, accuracyM: 10, speedMps: 9, headingDeg: 200, at: t });
  const past = (i: number, t: number): Fix => ({ ...at(i, t), lat: stops[i].lat! - 0.0004 });   // ~40 m on: behind them
  const guideAt = (fixes: Fix[], readOut = true): Guide => {
    let mem = freshMemory(plan);
    for (const f of fixes) mem = advance(plan, mem, f);
    return { ...guideView(plan, mem, fixes[fixes.length - 1].at), readOut, setReadOut: vi.fn() };
  };

  it("on the bus: the countdown big, only the lines for now, the landmark photo only when near", async () => {
    const guide = guideAt([at(0, 1_000), past(1, 61_000)]);                     // just past the first stop
    const { container } = render(<StepCard step={step} loaded={demo} index={2} total={4} tripId="T-1" guide={guide} />);
    expect(screen.getByRole("status").textContent).toBe("5 more stops");
    expect(document.getElementById("step-text")?.textContent).toBe("Stay on the bus");
    await waitFor(() => expect(photos(container)).toHaveLength(1));         // the step photo; Waterway Point isn't near yet
    expect(screen.queryByText("Look for Waterway Point")).toBeNull();
  });

  it("near the landmark its line and photo come up; at the end, press the bell and get off", async () => {
    const near = guideAt([at(0, 1_000), at(3, 121_000)]);
    const { container, rerender } = render(<StepCard step={step} loaded={demo} index={2} total={4} tripId="T-1" guide={near} />);
    expect(screen.getByText("Look out for Waterway Point on your left")).toBeTruthy();
    // its photo takes the step photo's place while it's near: what to look for now
    await waitFor(() => expect(photos(container)).toHaveLength(1));
    expect(photos(container)[0]).toContain("waterway-point.svg");

    const bell = guideAt([at(0, 1_000), at(3, 121_000), past(5, 241_000)]);
    rerender(<StepCard step={step} loaded={demo} index={2} total={4} tripId="T-1" guide={bell} />);
    expect(screen.getByRole("status").textContent).toBe("Press the bell now");
    expect(screen.getAllByRole("listitem").map((li) => li.textContent)).toEqual(["Stay on the bus", "Press the bell", "Get off at Block 221"]);
  });

  it("'Show all' shows the whole step again, and the switch turns reading aloud off", () => {
    const guide = guideAt([at(0, 1_000), past(1, 61_000)]);
    render(<StepCard step={step} loaded={demo} index={2} total={4} tripId="T-1" guide={guide} />);
    fireEvent.click(screen.getByRole("button", { name: /show all instructions \(4 more\)/i }));
    expect(screen.getAllByRole("listitem").map((li) => li.textContent).slice(0, 5)).toEqual([
      "Stay on the bus", "Count 6 stops", "Look out for Waterway Point on your left", "Press the bell", "Get off at Block 221",
    ]);
    expect(screen.getByRole("status").textContent).toBe("5 more stops");     // the countdown stays
    fireEvent.click(screen.getByRole("button", { name: "Read aloud as I go: on" }));
    expect(guide.setReadOut).toHaveBeenCalledWith(false);
  });

  it("Listen reads what is on screen now: the prompt, then the lines shown", () => {
    const said: string[] = [];
    vi.stubGlobal("speechSynthesis", { cancel: vi.fn(), speak: vi.fn((u: { text: string }) => said.push(u.text)) });
    vi.stubGlobal("SpeechSynthesisUtterance", class { rate = 1; onend = null; onerror = null; constructor(public text: string) {} });
    try {
      render(<StepCard step={step} loaded={demo} index={2} total={4} tripId="T-1" guide={guideAt([at(0, 1_000), past(1, 61_000)])} />);
      fireEvent.click(screen.getByRole("button", { name: /listen/i }));
      expect(said).toEqual(["Stay on the bus. 5 more stops. Stay on the bus."]);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
