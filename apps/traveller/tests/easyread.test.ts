import { easyReadPoints, landmarkCaption } from "../src/lib/easyRead";

describe("easyReadPoints", () => {
  // The same cases as the caregiver app's tests (apps/caregiver/tests/easy-read-points.test.ts), so the traveller
  // sees exactly the points the caregiver checked in the editor.
  it("splits the wording into one action per point, like the caregiver's editor", () => {
    expect(easyReadPoints("Go to Sengkang Interchange. Wait for bus number 163. Tap your card!")).toEqual([
      "Go to Sengkang Interchange",
      "Wait for bus number 163",
      "Tap your card!",
    ]);
  });

  it("doesn't split inside a name that has a full stop", () => {
    expect(easyReadPoints("Walk to St. Andrew's Village. Wait at Opp. Block 5.")).toEqual([
      "Walk to St. Andrew's Village",
      "Wait at Opp. Block 5",
    ]);
  });

  it("keeps numbers like 1.5 together and copes with nothing", () => {
    expect(easyReadPoints("Walk 1.5 km. Turn left.")).toEqual(["Walk 1.5 km", "Turn left"]);
    expect(easyReadPoints("")).toEqual([]);
    expect(easyReadPoints(null)).toEqual([]);
  });
});

describe("landmarkCaption", () => {
  it("with its own photo: just what to look for", () => {
    expect(landmarkCaption({ name: "Oasis Terraces", photoUrl: "media/a.jpg" }, true)).toBe("Look for Oasis Terraces");
  });

  it("no photo of its own: says which photo shows it, in the caregiver app's words", () => {
    expect(landmarkCaption({ name: "Block 221", photoUrl: null, sameView: "step" }, true)).toBe(
      "Look for Block 221: it is in the step photo above.");
    expect(landmarkCaption({ name: "Kopitiam", photoUrl: null, sameView: "Oasis Terraces" }, true)).toBe(
      "Look for Kopitiam: it is in the photo of Oasis Terraces.");
  });

  it("the pack doesn't say which photo (the Journey Service sends only name and photoUrl): 'the photo above'", () => {
    expect(landmarkCaption({ name: "Block 221", photoUrl: null }, true)).toBe("Look for Block 221: it is in the photo above.");
  });

  it("never points at a step photo that doesn't exist", () => {
    expect(landmarkCaption({ name: "Block 221", photoUrl: null, sameView: "step" }, false)).toBe("Look for Block 221.");
    expect(landmarkCaption({ name: "Block 221", photoUrl: null }, false)).toBe("Look for Block 221.");
  });
});
