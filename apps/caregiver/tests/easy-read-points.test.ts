import { easyReadPoints, fromBulletLines, isRoughPhoto, landmarkSeenIn, namedLandmarks, photoHint, roughPhotoHint, toBulletLines } from "../src/planner/format";

describe("photoHint", () => {
  const stop = (code: string | null) => ({ name: "X", code, lat: 1.3, lng: 103.8 });

  it("asks for the photo Street View can't take", () => {
    expect(photoHint({ mode: "train", to: stop("NE12") })).toMatch(/gantry or the platform/);
    expect(photoHint({ mode: "walk", to: stop("NE16") })).toMatch(/entrance or the gantry/);
    expect(photoHint({ mode: "bus", to: stop("67009") })).toMatch(/bus stop/);
    expect(photoHint({ mode: "walk", to: stop("67009") })).toMatch(/where this walk ends/);   // to a bus stop
    expect(photoHint({ mode: "walk", to: stop(null) })).toMatch(/where this walk ends/);
    expect(photoHint({ mode: "walk", from: stop("NE6"), to: stop("NS24") })).toMatch(/signs to the other line/);
    expect(photoHint({ mode: "walk", from: stop("67009"), to: stop("NE16") })).toMatch(/entrance or the gantry/);
  });

  it("asks the caregiver to check a rough photo (only an old one was found)", () => {
    expect(isRoughPhoto("media/streetview/v2/rough/1.30400_103.83180.jpg")).toBe(true);
    expect(isRoughPhoto("media/streetview/v2/1.30400_103.83180.jpg")).toBe(false);
    expect(isRoughPhoto("media/uploads/abc.jpg")).toBe(false);
    expect(isRoughPhoto(null)).toBe(false);
    expect(roughPhotoHint({ mode: "walk", to: stop("NE16") })).toMatch(/old street photo.*entrance or the gantry/);
    expect(roughPhotoHint({ mode: "bus", to: stop("67009") })).toMatch(/the bus stop/);
  });
});

describe("the editor's bullet lines", () => {
  const saved = "Go to Sengkang Interchange. Wait for bus number 163. Tap your card!";

  it("shows one bullet per line and saves them back as the same sentences", () => {
    expect(toBulletLines(saved)).toBe("• Go to Sengkang Interchange\n• Wait for bus number 163\n• Tap your card!");
    expect(fromBulletLines(toBulletLines(saved))).toBe(saved);
  });

  it("accepts lines without a bullet, other bullet marks and blank lines", () => {
    expect(fromBulletLines("Get on the bus\n\n- Count 3 stops.\n*   Get off at Block 126A\n• ")).toBe(
      "Get on the bus. Count 3 stops. Get off at Block 126A.");
    expect(fromBulletLines("• ")).toBe("");
    expect(toBulletLines(null)).toBe("");
  });
});

describe("easyReadPoints", () => {
  it("makes one point per sentence, without the full stop", () => {
    expect(easyReadPoints("Go to Sengkang Interchange. Wait for bus number 163. Tap your card. Count 3 stops."))
      .toEqual(["Go to Sengkang Interchange", "Wait for bus number 163", "Tap your card", "Count 3 stops"]);
  });

  it("keeps a name with a short form in one point", () => {
    expect(easyReadPoints("Walk to St. Andrew's Cathedral. It takes about 4 minutes."))
      .toEqual(["Walk to St. Andrew's Cathedral", "It takes about 4 minutes"]);
  });

  it("handles text without a full stop, extra spaces and nothing at all", () => {
    expect(easyReadPoints("Walk to [place 1]")).toEqual(["Walk to [place 1]"]);
    expect(easyReadPoints("  Get on the bus.   Tap your card!  ")).toEqual(["Get on the bus", "Tap your card!"]);
    expect(easyReadPoints(null)).toEqual([]);
    expect(easyReadPoints("   ")).toEqual([]);
  });
});

describe("namedLandmarks", () => {
  it("keeps only the landmarks the wording still names", () => {
    const marks = [{ name: "City Plaza" }, { name: "Geylang Telephone Exchange" }];
    expect(namedLandmarks(marks, "Look out for city plaza on your left. Press the bell.")).toEqual([{ name: "City Plaza" }]);
    expect(namedLandmarks(marks, null)).toEqual([]);
    expect(namedLandmarks(null, "anything")).toEqual([]);
  });
});

describe("landmarkSeenIn", () => {
  it("points to the photo that already shows it", () => {
    expect(landmarkSeenIn({ name: "Punggol Coast Mall", sameView: "step" })).toBe(
      "Look for Punggol Coast Mall: it is in the step photo above.");
    expect(landmarkSeenIn({ name: "Block 12", sameView: "City Plaza" })).toBe("Look for Block 12: it is in the photo of City Plaza.");
  });
});
