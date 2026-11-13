// Who is signed in (issue 1.5), kept in localStorage so a reload stays signed in. The ID token (sent to the
// services as `Authorization: Bearer`) lasts an hour; it is refreshed before it runs out, for up to 30 days, after
// which the person signs in again. The same file is in both apps; keep them the same.
import { claimsOf, CognitoClient, CognitoError, type Tokens } from "./cognito";

export type Role = "caregiver" | "traveller";

export interface Session {
  userId: string;          // G-003 or U-042: the token's custom:userId
  role: Role;
  email: string | null;
  idToken: string;
  refreshToken: string;
  expiresAt: number;       // ms, when idToken runs out
}

const REFRESH_EARLY_MS = 5 * 60_000;
const ID = /^[GU]-[A-Za-z0-9]{1,40}$/;

export class WrongAccountError extends Error {}

interface Storage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** A session from Cognito's tokens, or an error if the account can't use this app. */
export function sessionFrom(tokens: Tokens, refreshToken: string, role: Role): Session {
  const claims = claimsOf(tokens.IdToken);
  const groups = claims["cognito:groups"];
  const userId = claims["custom:userId"];
  const roles = (Array.isArray(groups) ? groups : []).filter((g): g is Role => g === "caregiver" || g === "traveller");
  if (roles.length !== 1 || roles[0] !== role) {
    throw new WrongAccountError(role === "caregiver"
      ? "That's a traveller's login. Caregivers sign in with their email."
      : "That's a caregiver's login. Use the traveller ID and setup code from your caregiver.");
  }
  if (typeof userId !== "string" || !ID.test(userId)) {
    throw new WrongAccountError("This account isn't set up yet. Try again in a minute, or ask for help.");
  }
  const exp = typeof claims.exp === "number" ? claims.exp * 1000 : Date.now() + tokens.ExpiresIn * 1000;
  return { userId, role, email: typeof claims.email === "string" ? claims.email : null,
           idToken: tokens.IdToken, refreshToken, expiresAt: exp };
}

export class LoginSession {
  private current: Session | null;
  private listeners = new Set<() => void>();
  private refreshing: Promise<void> | null = null;

  constructor(
    readonly client: CognitoClient | null,
    readonly role: Role,
    private storage: Storage | null = safeLocalStorage(),
    private key = `careroute.login.${role}`,
    private now: () => number = Date.now,
  ) {
    this.current = client ? this.load() : null;
  }

  /** Logins are on (the Cognito settings are there). */
  get enabled(): boolean {
    return this.client !== null;
  }

  get session(): Session | null {
    return this.current;
  }

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  /** The ID token as it is now, for callers that can't wait (an EventSource address). May have run out if the
   * phone was offline; `freshIdToken` refreshes it first. */
  idToken = (): string | null => this.current?.idToken ?? null;

  /** The ID token, refreshed first if it runs out within 5 minutes. Null when signed out. */
  freshIdToken = async (): Promise<string | null> => {
    if (this.current && this.current.expiresAt - this.now() < REFRESH_EARLY_MS) await this.refresh();
    return this.current?.idToken ?? null;
  };

  async signIn(username: string, password: string): Promise<Session> {
    const tokens = await this.need().signIn(username.trim(), password);
    const session = sessionFrom(tokens, tokens.RefreshToken ?? "", this.role);
    this.set(session);
    return session;
  }

  signOut(): void {
    const old = this.current;
    this.set(null);
    if (old?.refreshToken) this.client?.revoke(old.refreshToken).catch(() => { /* it runs out by itself */ });
  }

  private refresh(): Promise<void> {
    this.refreshing ??= (async () => {
      const old = this.current;
      if (!old) return;
      try {
        const tokens = await this.need().refresh(old.refreshToken);
        if (this.current === old) this.set(sessionFrom(tokens, old.refreshToken, this.role));
      } catch (e) {
        // Signed out elsewhere, a new setup code, or 30 days passed: sign in again. Anything else (no network)
        // keeps the old token; the next request tries again.
        if (e instanceof CognitoError && e.code === "NotAuthorizedException" && this.current === old) this.set(null);
        if (e instanceof WrongAccountError) this.set(null);
      }
    })().finally(() => { this.refreshing = null; });
    return this.refreshing;
  }

  private need(): CognitoClient {
    if (!this.client) throw new Error("Logins are off (no VITE_COGNITO_* settings)");
    return this.client;
  }

  private set(session: Session | null) {
    this.current = session;
    try {
      if (session) this.storage?.setItem(this.key, JSON.stringify(session));
      else this.storage?.removeItem(this.key);
    } catch { /* private mode: signed in until the page closes */ }
    this.listeners.forEach((fn) => fn());
  }

  private load(): Session | null {
    try {
      const saved = JSON.parse(this.storage?.getItem(this.key) ?? "null") as Session | null;
      return saved && saved.role === this.role && ID.test(saved.userId) && saved.refreshToken ? saved : null;
    } catch {
      return null;
    }
  }
}

function safeLocalStorage(): Storage | null {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}
