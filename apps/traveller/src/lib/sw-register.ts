// Registers the service worker (production builds only; in `npm run dev` it would get in
// the way of hot reload). Exposes whether the app itself is stored for offline use.
import { Workbox } from "workbox-window";

let ready = false;
const listeners = new Set<(ready: boolean) => void>();

export const appShell = {
  get ready() {
    return ready;
  },
  onChange(fn: (ready: boolean) => void) {
    listeners.add(fn);
    fn(ready);
    return () => listeners.delete(fn);
  },
};

function set(v: boolean) {
  ready = v;
  listeners.forEach((fn) => fn(v));
}

export function registerServiceWorker(): void {
  if (!import.meta.env.PROD || !("serviceWorker" in navigator)) return;
  const wb = new Workbox("/sw.js");
  wb.addEventListener("activated", () => set(true));
  wb.addEventListener("controlling", () => set(true));
  void wb
    .register()
    .then((reg) => {
      if (reg?.active) set(true);
    })
    .catch((e) => console.warn("CareRoute: service worker not registered", e));
}
