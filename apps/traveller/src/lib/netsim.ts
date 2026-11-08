// "Pretend there's no signal" switch for demos and testing (debug panel).
// While on, the app behaves exactly as if the phone were offline: check-ins pile up
// in the outbox, then go as one batch when it's switched off again.
import type { SendResult, Transport } from "./api";

let simulatedOffline = false;
const listeners = new Set<(offline: boolean) => void>();

export const netsim = {
  get offline() {
    return simulatedOffline;
  },
  set(offline: boolean) {
    simulatedOffline = offline;
    listeners.forEach((fn) => fn(offline));
  },
  onChange(fn: (offline: boolean) => void) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
};

export const isOnline = () => !simulatedOffline && (typeof navigator === "undefined" || navigator.onLine);

const OFFLINE: SendResult = { kind: "retry", reason: "no signal (simulated)" };

/** Wraps the real transport so requests fail like a dropped connection while simulated offline. */
export function withNetsim(t: Transport): Transport {
  return {
    postCheckin: (c, o) => (simulatedOffline ? Promise.resolve(OFFLINE) : t.postCheckin(c, o)),
    postBatch: (b) => (simulatedOffline ? Promise.resolve(OFFLINE) : t.postBatch(b)),
    post: (p, b) => (simulatedOffline ? Promise.resolve(OFFLINE) : t.post(p, b)),
  };
}
