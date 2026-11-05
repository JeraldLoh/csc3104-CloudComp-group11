// "Where are you?" (issue 5.5): asks the Alert Service whether a caregiver is waiting for an answer. Nothing can
// reach the phone until push notifications exist, so the app asks every few seconds while a trip is open.
// The answer goes through the request queue like Help, so it keeps trying with no signal.
import { API_BASE } from "../config";
import { isOnline } from "./netsim";

/** A question waiting for the traveller (the Alert Service's where-are-you request). */
export interface WhereQuestion {
  requestId: string;
  askedAt: string;
}

export type WhereAnswer = "ok" | "help";

export const WHERE_CHECK_MS = 5000;

/** The waiting question, or null if there's none or it couldn't be checked: the next check tries again. */
export async function fetchQuestion(travellerId: string, fetchImpl: typeof fetch = fetch,
                                    timeoutMs = 5000): Promise<WhereQuestion | null> {
  if (!isOnline()) return null;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetchImpl(`${API_BASE}/api/where-are-you?travellerId=${encodeURIComponent(travellerId)}`,
      { signal: controller.signal });
    if (!res.ok) return null;
    const body = (await res.json()) as WhereQuestion | null;
    return body?.requestId ? body : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}
