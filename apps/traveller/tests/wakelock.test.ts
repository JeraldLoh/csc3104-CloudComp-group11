import { ScreenWakeLock, type WakeLockState } from "../src/lib/wakelock";

class FakeSentinel {
  released = false;
  private fns: (() => void)[] = [];
  addEventListener(_t: "release", fn: () => void) {
    this.fns.push(fn);
  }
  async release() {
    this.released = true;
    this.fns.forEach((f) => f());
  }
}

function fakeApi() {
  const made: FakeSentinel[] = [];
  return { made, api: { request: vi.fn(async () => { const s = new FakeSentinel(); made.push(s); return s; }) } };
}

const setVisibility = (v: "visible" | "hidden") => {
  Object.defineProperty(document, "visibilityState", { value: v, configurable: true });
  document.dispatchEvent(new Event("visibilitychange"));
};

describe("ScreenWakeLock", () => {
  afterEach(() => setVisibility("visible"));

  it("keeps the screen on during the trip and lets it go on arrival", async () => {
    const { api, made } = fakeApi();
    const states: WakeLockState[] = [];
    const lock = new ScreenWakeLock(api, (s) => states.push(s));
    await lock.enable();
    expect(states.at(-1)).toBe("on");
    await lock.disable();
    expect(made[0].released).toBe(true);
    expect(states.at(-1)).toBe("off");
  });

  it("asks again when the traveller comes back to the app (the browser drops it when hidden)", async () => {
    const { api, made } = fakeApi();
    const lock = new ScreenWakeLock(api);
    await lock.enable();
    setVisibility("hidden");
    await made[0].release(); // what the browser does
    setVisibility("visible");
    await new Promise((r) => setTimeout(r, 0));
    expect(api.request).toHaveBeenCalledTimes(2);
    await lock.disable();
  });

  it("doesn't ask again after the trip has ended", async () => {
    const { api } = fakeApi();
    const lock = new ScreenWakeLock(api);
    await lock.enable();
    await lock.disable();
    setVisibility("visible");
    await new Promise((r) => setTimeout(r, 0));
    expect(api.request).toHaveBeenCalledTimes(1);
  });

  it("reports unsupported browsers instead of failing", async () => {
    const states: WakeLockState[] = [];
    await new ScreenWakeLock(undefined, (s) => states.push(s)).enable();
    expect(states).toEqual(["unsupported"]);
  });

  it("a refused request (e.g. battery saver) isn't an error", async () => {
    const states: WakeLockState[] = [];
    const api = { request: vi.fn(async () => { throw new DOMException("denied", "NotAllowedError"); }) };
    await new ScreenWakeLock(api, (s) => states.push(s)).enable();
    expect(states).toEqual(["off"]);
  });
});
