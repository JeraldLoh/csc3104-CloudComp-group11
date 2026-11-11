// Calls to the Alert Service (contracts/api.md, Member 5). Paths are relative: in development
// vite.config.ts forwards them to the Alert Service.
//
// Who is calling: with logins on (issue 1.5, src/auth), the signed-in caregiver's Cognito token as
// `Authorization: Bearer`. Without them, the "Act as" caregiver as X-Dev-User (AUTH_MODE=dev), as for the Journey Service.
import { login } from "../auth/current";
import type { Incident } from "./incidents";

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

export async function call<T>(path: string, caregiverId: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (login.enabled) {
    const token = await login.freshIdToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
  } else {
    headers.set("X-Dev-User", caregiverId);
  }
  if (init.body) headers.set("Content-Type", "application/json");
  const resp = await fetch(path, { ...init, headers });
  if (!resp.ok) {
    const body = await resp.json().catch(() => null);
    throw new ApiError(resp.status, body?.error?.message ?? `${resp.status} ${resp.statusText}`);
  }
  return resp.json() as Promise<T>;
}

export function fetchIncidents(travellerId: string, caregiverId: string): Promise<Incident[]> {
  // The most the service gives: the default of 50 newest could push an old unresolved alert off the list.
  return call(`/api/incidents?travellerId=${encodeURIComponent(travellerId)}&limit=200`, caregiverId);
}

// Incident IDs contain '#', so they must be URL-encoded in the path.
export function acknowledge(incidentId: string, caregiverId: string): Promise<Incident> {
  return call(`/api/incidents/${encodeURIComponent(incidentId)}/ack`, caregiverId, { method: "POST" });
}

export function resolve(incidentId: string, caregiverId: string): Promise<Incident> {
  return call(`/api/incidents/${encodeURIComponent(incidentId)}/resolve`, caregiverId, { method: "POST" });
}

/** A browser's EventSource can't send headers, so the login goes in the address. Each (re)connect asks again, and
 * src/auth keeps the token fresh while the page is open. */
export function streamUrl(caregiverId: string): string {
  if (login.enabled) return `/api/stream?token=${encodeURIComponent(login.idToken() ?? "")}`;
  return `/api/stream?devUser=${encodeURIComponent(caregiverId)}`;
}
