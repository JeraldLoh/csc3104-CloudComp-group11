// The traveller app's login (issue 1.5). Off unless VITE_COGNITO_REGION and VITE_COGNITO_CLIENT_ID are set; then the
// phone signs in once with the traveller ID and setup code from their caregiver, and every request carries the token.
import { useSyncExternalStore } from "react";
import { CognitoClient, readConfig } from "./cognito";
import { LoginSession, type Session } from "./session";

const config = readConfig();
export const login = new LoginSession(config ? new CognitoClient(config) : null, "traveller");

// Keep the token fresh: every minute, and as soon as the phone is back online or the app is opened again
// (a token that ran out while offline would otherwise hold up the check-ins waiting to be sent).
if (login.enabled && typeof window !== "undefined") {
  const freshen = () => void login.freshIdToken();
  setInterval(freshen, 60_000);
  window.addEventListener("online", freshen);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") freshen(); });
}

export function useLogin(): Session | null {
  return useSyncExternalStore(login.subscribe, () => login.session);
}

/** `Authorization` for a request to the services, refreshed first if need be; empty with logins off. */
export async function authHeaders(): Promise<Record<string, string>> {
  const token = login.enabled ? await login.freshIdToken() : null;
  return token ? { Authorization: `Bearer ${token}` } : {};
}
