// Keeps the screen on during a trip (issue 2.2), using the Screen Wake Lock API.
//
// Why: if the screen turns off, the browser treats the app as "in the background": GPS
// and timers slow down or stop (iOS stops them), so check-ins stop and the traveller
// has to unlock the phone to see their next step. Keeping the screen on avoids both.
//
// The browser releases the lock by itself whenever the app is hidden (another app,
// phone locked by the button), so it's requested again each time the app comes back.
// Costs some battery; the check-ins report batteryPct so the caregiver can see it.
type Sentinel = { released: boolean; release(): Promise<void>; addEventListener(t: "release", fn: () => void): void };
type WakeLockApi = { request(type: "screen"): Promise<Sentinel> };

export type WakeLockState = "on" | "off" | "unsupported";

export class ScreenWakeLock {
  private sentinel: Sentinel | null = null;
  private wanted = false;
  private onVisible = () => {
    if (document.visibilityState === "visible" && this.wanted) void this.acquire();
  };

  constructor(
    private api: WakeLockApi | undefined = (navigator as Navigator & { wakeLock?: WakeLockApi }).wakeLock,
    private onChange: (s: WakeLockState) => void = () => {},
  ) {}

  get supported() {
    return !!this.api;
  }

  async enable(): Promise<void> {
    this.wanted = true;
    if (!this.api) return this.onChange("unsupported");
    document.addEventListener("visibilitychange", this.onVisible);
    await this.acquire();
  }

  async disable(): Promise<void> {
    this.wanted = false;
    document.removeEventListener("visibilitychange", this.onVisible);
    const s = this.sentinel;
    this.sentinel = null;
    if (s && !s.released) await s.release().catch(() => {});
    this.onChange(this.api ? "off" : "unsupported");
  }

  private async acquire(): Promise<void> {
    if (!this.api || (this.sentinel && !this.sentinel.released)) return;
    try {
      const s = await this.api.request("screen");
      this.sentinel = s;
      this.onChange("on");
      s.addEventListener("release", () => {
        if (this.sentinel === s) this.onChange("off");
      });
    } catch {
      // Refused (battery saver, page not visible yet): tried again when the app is next shown.
      this.onChange("off");
    }
  }
}
