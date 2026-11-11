// The caregiver app's login (issue 1.5). Off unless VITE_COGNITO_REGION and VITE_COGNITO_CLIENT_ID are set; then
// every request carries the signed-in caregiver's token instead of the "Act as" header.
import { useSyncExternalStore } from "react";
import { CognitoClient, readConfig } from "./cognito";
import { LoginSession, type Session } from "./session";

const config = readConfig();
export const login = new LoginSession(config ? new CognitoClient(config) : null, "caregiver");

// Keep the token fresh while the page is open, so the live alert stream reconnects with a valid one.
if (login.enabled && typeof window !== "undefined") setInterval(() => void login.freshIdToken(), 60_000);

export function useLogin(): Session | null {
  return useSyncExternalStore(login.subscribe, () => login.session);
}
