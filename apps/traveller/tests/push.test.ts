import { askToNotify, pushToken } from "../src/lib/push";

// The tests run without firebase-web-config.json, as a teammate without the Firebase project does.
describe("push without Firebase set up", () => {
  it("never asks for permission and has no token", async () => {
    expect(await askToNotify()).toBe(false);
    expect(await pushToken()).toBeNull();
  });
});
