import type { SenderState } from "../lib/sender";

export type Connection = "ok" | "catching-up" | "offline" | "login";

export function connectionOf(s: SenderState | null, offline: boolean): Connection {
  if (!s) return "ok";
  if (s.authRequired) return "login";
  if (offline || s.lastError === "offline") return s.pending > 0 || offline ? "offline" : "ok";
  if (s.consecutiveFailures > 0 || s.pending > 1) return "catching-up";
  return "ok";
}

/** One short, calm line about whether the caregiver can see the trip. */
export function ConnectionStatus({ state, offline, caregiver }: { state: SenderState | null; offline: boolean; caregiver: string }) {
  const c = connectionOf(state, offline);
  const text = {
    ok: `${caregiver} can see your trip`,
    "catching-up": `Sending your trip to ${caregiver}`,
    offline: "No signal. That's OK, keep going.",
    login: `Ask ${caregiver} to sign you in again`,
  }[c];
  return (
    <p className={`status status--${c}`} role="status" aria-live="polite">
      <span className="status__dot" aria-hidden />
      {text}
    </p>
  );
}
