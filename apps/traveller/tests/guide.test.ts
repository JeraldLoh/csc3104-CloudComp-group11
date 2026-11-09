import { advance, announcements, freshMemory, GUIDE, guideView, metres, planGuide, shownKeys, type GuideMemory, type GuidePlan } from "../src/lib/guide";
import type { Fix } from "../src/lib/tracker";
import { validatePack } from "../src/lib/offline";
import type { RoutePackStep } from "../src/types";
// A pack made by the Journey Service's own build_pack (tests/fixtures/journey_pack.py)
import journeyPack from "./fixtures/journey-pack.json";

// A bus ride straight down a road: 7 stops (get on at the first, 6 stops to ride), about 400 m apart.
const LNG = 103.9024;
const latAt = (m: number) => 1.4052 - m / 110_574;          // `m` metres along the ride from the first stop
const STOPS = ["Punggol Interchange", "Block 268", "Block 261", "Block 255", "Block 230", "Block 222", "Block 221"]
  .map((name, i) => ({ name, lat: latAt(i * 400), lng: LNG }));

const JOURNEY_WORDING = "Find bus stop 65009 at Punggol Interchange. Check the number on the pole. "
  + "Raise your hand for bus 190 to Changi Village. It comes every 5 to 8 minutes. Tap your card to get on. "
  + "The ride takes about 12 minutes. Look out for Oasis Terraces on your right. "
  + "After Block 222, press the bell. Get off at Block 221. Tap your card to get off.";

const busStep = (over: Partial<RoutePackStep> = {}): RoutePackStep => ({
  stepIndex: 1, mode: "bus", easyRead: JOURNEY_WORDING, lat: STOPS[6].lat, lng: LNG, underground: false,
  photoUrl: null, audioUrl: null, backup: "If bus 190 does not come in 20 minutes, take bus 83.",
  stops: STOPS,
  // Oasis Terraces is seen from about 1 km along the ride
  landmarks: [{ name: "Oasis Terraces", photoUrl: "oasis.jpg", lat: latAt(1000), lng: LNG + 0.002, seenFrom: [latAt(1000), LNG] }],
  ...over,
});

let clock = 1_000_000;
const fixAt = (m: number, over: Partial<Fix> = {}): Fix => {
  clock += 120_000;                                           // 2 min between fixes: bus-like jumps of a few hundred m are fine
  return { lat: latAt(m), lng: LNG, accuracyM: 10, speedMps: 8, headingDeg: 180, at: clock, ...over };
};

/** Runs fixes through the guide; returns the memory and the view after the last one. */
function ride(plan: GuidePlan | null, metresAlong: number[], mem: GuideMemory = freshMemory(plan)) {
  for (const m of metresAlong) mem = advance(plan, mem, fixAt(m));
  return { mem, view: guideView(plan, mem, mem.at ?? clock) };
}
const texts = (v: { lines: { text: string }[] }) => v.lines.map((l) => l.text);

describe("planGuide: which line is for when", () => {
  it("splits the Journey Service's bus wording into getting on, the ride, the landmark, the bell and getting off", () => {
    const plan = planGuide(busStep(), "R-1-v1")!;
    expect(plan.lines.map((l) => l.kind)).toEqual([
      "before", "before", "before", "before", "before",       // find, check, raise your hand, how often, tap to get on
      "during", "landmark", "bell", "off", "off",
    ]);
    expect(plan.lines[6].landmark).toBe(0);
    expect(plan.along![6]).toBeCloseTo(2400, -1);
  });

  it("follows the shorter wording too (the caregiver's or the AI's): 'Count 6 stops' is replaced by the countdown", () => {
    const easyRead = "Wait for bus number 190. Tap your card. Count 6 stops. Look out for Oasis Terraces on your right. "
      + "Press the bell. Get off at Block 221.";
    const plan = planGuide(busStep({ easyRead }), "R-1-v1")!;
    expect(plan.lines.map((l) => l.kind)).toEqual(["before", "before", "count", "landmark", "bell", "off"]);
  });

  it("shows a step whole when it can't follow it: a train, or a bus with no stop or landmark positions", () => {
    expect(planGuide(busStep({ mode: "train" }), "v")).toBeNull();
    expect(planGuide(busStep({ stops: undefined, landmarks: [{ name: "Oasis Terraces", photoUrl: null }] }), "v")).toBeNull();
    // one stop without a position: the ride isn't counted
    const gap = STOPS.map((s, i) => (i === 3 ? { ...s, lat: null, lng: null } : s));
    expect(planGuide(busStep({ stops: gap, landmarks: [] }), "v")).toBeNull();
  });

  it("without the stops' positions it still waits for the landmark, and shows the other lines at once", () => {
    const plan = planGuide(busStep({ stops: undefined }), "v")!;
    expect(plan.lines.map((l) => l.kind)).toEqual(["during", "during", "during", "during", "during",
      "during", "landmark", "during", "during", "during"]);
    const { view } = ride(plan, [0]);
    expect(view.on).toBe(true);
    expect(view.prompt).toBeNull();
    expect(texts(view)).not.toContain("Look out for Oasis Terraces on your right");
    expect(view.hidden).toBe(1);
  });
});

describe("guideView: one thing at a time on the bus", () => {
  const plan = planGuide(busStep(), "R-1-v1");

  it("waiting at the stop: only the lines for getting on", () => {
    const { view } = ride(plan, [0, 5]);
    expect(view.on).toBe(true);
    expect(view.started).toBe(false);
    expect(view.prompt).toBeNull();
    expect(texts(view)).toEqual([
      "Find bus stop 65009 at Punggol Interchange", "Check the number on the pole",
      "Raise your hand for bus 190 to Changi Village", "It comes every 5 to 8 minutes", "Tap your card to get on",
    ]);
    expect(view.hidden).toBe(5);
  });

  it("on the bus: the getting-on lines go, and a countdown of the stops left", () => {
    const { view } = ride(plan, [0, 150]);
    expect(view.started).toBe(true);
    expect(view.prompt).toEqual({ key: "left-6", title: "6 more stops", say: "Stay on the bus. 6 more stops." });
    expect(texts(view)).toEqual(["The ride takes about 12 minutes"]);
  });

  it("counts a stop as soon as the bus is past it, whether or not it stopped there", () => {
    expect(ride(plan, [0, 150, 380]).view.stopsLeft).toBe(6);    // not yet at Block 268
    expect(ride(plan, [0, 150, 430]).view.stopsLeft).toBe(5);    // just past it
    expect(ride(plan, [0, 150, 430, 900]).view.prompt?.title).toBe("4 more stops");
  });

  it("the landmark's line comes up as the bus gets near it, and goes once it's behind", () => {
    const near = ride(plan, [0, 150, 750]).view;                // 250 m before Oasis Terraces
    expect(texts(near)).toContain("Look out for Oasis Terraces on your right");
    expect(near.landmarkShown).toEqual([true]);
    const past = ride(plan, [0, 150, 750, 1500]).view;
    expect(texts(past)).not.toContain("Look out for Oasis Terraces on your right");
    expect(past.landmarkShown).toEqual([false]);
  });

  it("2 stops left: the bell line ('After Block 222, press the bell')", () => {
    const { view } = ride(plan, [0, 150, 1650]);
    expect(view.stopsLeft).toBe(2);
    expect(texts(view)).toEqual(["The ride takes about 12 minutes", "After Block 222, press the bell"]);
  });

  it("1 stop left: press the bell now, and how to get off; at the stop: get off here", () => {
    const bell = ride(plan, [0, 150, 2050]).view;
    expect(bell.prompt).toEqual({ key: "bell", title: "Press the bell now",
      say: "Press the bell now. Get off at the next stop, Block 221." });
    expect(texts(bell)).toEqual(["The ride takes about 12 minutes", "After Block 222, press the bell",
      "Get off at Block 221", "Tap your card to get off"]);
    const here = ride(plan, [0, 150, 2050, 2360]).view;
    expect(here.stopsLeft).toBe(0);
    expect(here.prompt?.title).toBe("Get off here");
  });

  it("one stop to ride: press the bell as soon as the bus moves off", () => {
    const short = planGuide(busStep({
      easyRead: "Raise your hand for bus 190. Tap your card to get on. Press the bell when the bus moves off. "
        + "Get off at the next stop, Block 268.",
      stops: STOPS.slice(0, 2), landmarks: [],
    }), "v");
    const { view } = ride(short, [0, 150]);
    expect(view.prompt?.key).toBe("bell");
    expect(texts(view)).toEqual(["Press the bell when the bus moves off", "Get off at the next stop, Block 268"]);
  });

  it("a step that starts on the bus (no getting-on lines) counts down straight away", () => {
    const onBoard = planGuide(busStep({ easyRead: "Stay on the bus. Count 6 stops. Press the bell. Get off at Block 221." }), "v");
    const { view } = ride(onBoard, [0]);
    expect(view.started).toBe(true);
    expect(view.prompt?.title).toBe("6 more stops");
    expect(texts(view)).toEqual(["Stay on the bus"]);
  });
});

describe("advance: GPS that can't be trusted doesn't move the count", () => {
  const plan = planGuide(busStep(), "R-1-v1");

  it("ignores a jump faster than any bus, and an unsure fix", () => {
    let mem = ride(plan, [0, 150]).mem;
    const jump = advance(plan, mem, { ...fixAt(2300), at: mem.at! + 5_000 });   // 2 km in 5 s
    expect(jump).toBe(mem);
    mem = advance(plan, mem, fixAt(2300, { accuracyM: 300 }));
    expect(guideView(plan, mem, mem.at!).stopsLeft).toBe(6);
  });

  it("doesn't count back when GPS wobbles behind, nor move for a fix far off the road", () => {
    const { mem } = ride(plan, [0, 150, 900, 850]);
    expect(mem.progress).toBeCloseTo(900, -1);
    const off = advance(plan, mem, fixAt(1200, { lng: LNG + 0.01 }));            // 1 km to the side
    expect(off.progress).toBeCloseTo(900, -1);
  });

  it("with no recent fix, the whole step is shown again", () => {
    const { mem } = ride(plan, [0, 150]);
    expect(guideView(plan, mem, mem.at! + GUIDE.freshMs - 1).on).toBe(true);
    expect(guideView(plan, mem, mem.at! + GUIDE.freshMs + 1).on).toBe(false);
    expect(guideView(plan, freshMemory(plan), clock).on).toBe(false);           // no fix yet
  });

  it("a new route version starts again", () => {
    const { mem } = ride(plan, [0, 150, 900]);
    expect(guideView(planGuide(busStep(), "R-1-v2"), mem, mem.at!).on).toBe(false);
  });
});

describe("walk steps: landmarks when near", () => {
  const walk = planGuide({
    stepIndex: 0, mode: "walk", easyRead: "Walk to the bus stop at Punggol Interchange. Look for Waterway Point on your left.",
    lat: 1.4052, lng: 103.9024, underground: false, photoUrl: null, audioUrl: null, backup: null,
    landmarks: [{ name: "Waterway Point", photoUrl: "wp.jpg", lat: 1.4064, lng: 103.9022, seenFrom: [1.4045, 103.9018] }],
  }, "v");

  it("walking: the landmark line only within about 80 m of where it is seen from", () => {
    let mem = advance(walk, freshMemory(walk), { lat: 1.404, lng: 103.901, accuracyM: 8, speedMps: 1.2, headingDeg: 0, at: 1 });
    expect(texts(guideView(walk, mem, 1))).toEqual(["Walk to the bus stop at Punggol Interchange"]);
    mem = advance(walk, mem, { lat: 1.4043, lng: 103.9017, accuracyM: 8, speedMps: 1.2, headingDeg: 0, at: 2 });
    expect(metres([1.4043, 103.9017], [1.4045, 103.9018])).toBeLessThan(GUIDE.landmarkWalkM);
    expect(texts(guideView(walk, mem, 2))).toEqual(["Walk to the bus stop at Punggol Interchange", "Look for Waterway Point on your left"]);
  });
});

describe("announcements: what to read aloud", () => {
  const plan = planGuide(busStep(), "R-1-v1");

  it("each prompt and each landmark once, as they come", () => {
    const seen = new Set<string>();
    let run = ride(plan, [0]);
    expect(announcements(seen, run.view, plan)).toEqual([]);                    // waiting: nothing new to say
    run = ride(plan, [150], run.mem);
    expect(announcements(seen, run.view, plan)).toEqual(["Stay on the bus. 6 more stops."]);
    run = ride(plan, [750], run.mem);
    expect(announcements(seen, run.view, plan)).toEqual(["Stay on the bus. 5 more stops.", "Look out for Oasis Terraces on your right."]);
    expect(announcements(seen, run.view, plan)).toEqual([]);                    // same view again: nothing
    run = ride(plan, [2050], run.mem);
    expect(announcements(seen, run.view, plan)).toEqual(["Press the bell now. Get off at the next stop, Block 221."]);
  });

  it("reopening the trip mid-ride doesn't read out what is already on screen", () => {
    const { view } = ride(plan, [0, 150, 750]);
    const seen = new Set(shownKeys(view));
    expect(announcements(seen, view, plan)).toEqual([]);
  });
});

describe("a pack built by the Journey Service itself", () => {
  const pack = validatePack(structuredClone(journeyPack));
  const step = pack.steps[1];
  const plan = planGuide(step, pack.packVersion)!;
  const stopAt = (i: number, t: number, past = 0): Fix => ({
    lat: step.stops![i].lat! - past, lng: step.stops![i].lng!, accuracyM: 12, speedMps: 9, headingDeg: 200, at: t,
  });

  it("is followed: getting on first, then the countdown to Opposite Block 221", () => {
    expect(plan.lines.map((l) => l.kind)).toEqual(["before", "before", "count", "landmark", "bell", "off"]);
    let mem = advance(plan, freshMemory(plan), stopAt(0, 1_000));
    expect(texts(guideView(plan, mem, 1_000))).toEqual(["Wait for bus number 190", "Tap your card"]);
    mem = advance(plan, mem, stopAt(1, 90_000, 0.0003));
    expect(guideView(plan, mem, 90_000).prompt?.title).toBe("5 more stops");
    mem = advance(plan, mem, stopAt(5, 400_000, 0.0003));
    expect(guideView(plan, mem, 400_000).prompt?.say).toBe("Press the bell now. Get off at the next stop, Opposite Block 221.");
  });
});
