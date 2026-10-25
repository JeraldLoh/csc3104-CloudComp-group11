// Calls to the Alert Service (contracts/api.md, Member 5). Paths are relative: in development
// vite.config.ts forwards /api/incidents and /api/stream to the Alert Service.
import type { Incident } from "./incidents";

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, init);
  if (!resp.ok) {
    const body = await resp.json().catch(() => null);
    throw new Error(body?.error?.message ?? `${resp.status} ${resp.statusText}`);
  }
  return resp.json() as Promise<T>;
}

export function fetchIncidents(travellerId: string): Promise<Incident[]> {
  return call(`/api/incidents?travellerId=${encodeURIComponent(travellerId)}`);
}

// Incident IDs contain '#', so they must be URL-encoded in the path.
// X-User-Id: the caregiver acting, until login (Cognito, issue 1.5) exists.
export function acknowledge(incidentId: string, caregiverId: string): Promise<Incident> {
  return call(`/api/incidents/${encodeURIComponent(incidentId)}/ack`, {
    method: "POST",
    headers: { "X-User-Id": caregiverId },
  });
}

export function resolve(incidentId: string, caregiverId: string): Promise<Incident> {
  return call(`/api/incidents/${encodeURIComponent(incidentId)}/resolve`, {
    method: "POST",
    headers: { "X-User-Id": caregiverId },
  });
}

export const STREAM_URL = "/api/stream";
