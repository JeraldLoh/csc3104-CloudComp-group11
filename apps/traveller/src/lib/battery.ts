// Battery level for check-ins (batteryPct). Only some browsers (Chrome on Android)
// expose it; elsewhere it's null, which the contract allows.
interface BatteryManagerLike extends EventTarget {
  level: number;
}

let latest: number | null = null;

export function startBatteryWatch(): void {
  const nav = navigator as Navigator & { getBattery?: () => Promise<BatteryManagerLike> };
  if (!nav.getBattery) return;
  nav
    .getBattery()
    .then((b) => {
      const read = () => (latest = Math.round(b.level * 100));
      read();
      b.addEventListener("levelchange", read);
    })
    .catch(() => {});
}

export const batteryPct = (): number | null => latest;
