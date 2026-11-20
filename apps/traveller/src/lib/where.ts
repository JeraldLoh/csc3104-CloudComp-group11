// "Where are you?" (issue 5.5): asks the Alert Service whether a caregiver is waiting for an answer. Nothing can
// reach the phone until push notifications exist, so the app asks every few seconds while a trip is open.
// The answer goes through the request queue like Help, so it keeps trying with no signal.
import { API_BASE } from "../config";
import { authHeaders } from "../auth/current";
import { isOnline } from "./netsim";

/** A question waiting for the traveller (the Alert Service's where-are-you request). */
export interface WhereQuestion {
  requestId: string;
  askedAt: string;
}

export type WhereAnswer = "ok" | "help";

export const WHERE_CHECK_MS = 5000;

/** The waiting question, or null if there's none or it couldn't be checked: the next check tries again.
 * With `tripId`, only a question asked on this trip: one left over from an earlier trip isn't shown. */
export async function fetchQuestion(travellerId: string, fetchImpl: typeof fetch = fetch,
                                    timeoutMs = 5000, tripId?: string): Promise<WhereQuestion | null> {
  if (!isOnline()) return null;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const query = new URLSearchParams({ travellerId, ...(tripId ? { tripId } : {}) });
  try {
    const res = await fetchImpl(`${API_BASE}/api/where-are-you?${query}`,
      { signal: controller.signal, headers: await authHeaders() });   // headers: with logins on (issue 1.5)
    if (!res.ok) return null;
    const body = (await res.json()) as WhereQuestion | null;
    return body?.requestId ? body : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}
