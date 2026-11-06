import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { pushState, turnOnPush } from "../src/push/push";
import { PushToggle } from "../src/push/PushToggle";

// The tests run without firebase-web-config.json, as a teammate without the Firebase project does.
describe("push without Firebase set up", () => {
  it("is off, and turning it on does nothing", async () => {
    expect(await pushState()).toBe("off");
    expect(await turnOnPush("G-1")).toBe("off");
  });

  it("shows nothing on the dashboard", async () => {
    const { container } = render(<PushToggle caregiverId="G-1" />);
    await new Promise((r) => setTimeout(r, 0));
    expect(container.innerHTML).toBe("");
  });
});
