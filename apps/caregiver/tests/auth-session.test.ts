// Logins (issue 1.5): signing in, keeping the token fresh, and signing out, against a fake Cognito.
import { CognitoClient, CognitoError, explain, readConfig } from "../src/auth/cognito";
import { LoginSession, WrongAccountError } from "../src/auth/session";

const NOW = 1_800_000_000_000;

function jwt(claims: Record<string, unknown>): string {
  const b64 = (o: unknown) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64({ alg: "RS256" })}.${b64(claims)}.sig`;
}

function idToken(over: Record<string, unknown> = {}, expMs = NOW + 3_600_000) {
  return jwt({ "custom:userId": "G-003", "cognito:groups": ["caregiver"], email: "sam@example.com",
               exp: Math.floor(expMs / 1000), ...over });
}

type Reply = { status?: number; body: unknown } | Error;

/** A fake Cognito: answers each action from `replies`, and records what was asked. */
function fakeCognito(replies: Record<string, Reply[]>) {
  const calls: { action: string; body: Record<string, unknown> }[] = [];
  const fetchImpl = (async (_url: string, init: RequestInit) => {
    const action = String((init.headers as Record<string, string>)["X-Amz-Target"]).split(".")[1];
    const body = JSON.parse(String(init.body));
    calls.push({ action: body.AuthFlow ? `${action}:${body.AuthFlow}` : action, body });
    const reply = replies[body.AuthFlow ? `${action}:${body.AuthFlow}` : action]?.shift() ?? { body: {} };
    if (reply instanceof Error) throw reply;
    return new Response(JSON.stringify(reply.body), { status: reply.status ?? 200 });
  }) as typeof fetch;
  return { client: new CognitoClient({ region: "ap-southeast-1", clientId: "web" }, fetchImpl), calls };
}

class MemoryStorage {
  data = new Map<string, string>();
  getItem(k: string) { return this.data.get(k) ?? null; }
  setItem(k: string, v: string) { this.data.set(k, v); }
  removeItem(k: string) { this.data.delete(k); }
}

const signedIn = (token = idToken()) => ({ body: { AuthenticationResult: {
  IdToken: token, AccessToken: "access", RefreshToken: "refresh-1", ExpiresIn: 3600 } } });
const refreshed = (token: string) => ({ body: { AuthenticationResult: { IdToken: token, AccessToken: "a", ExpiresIn: 3600 } } });

let now = NOW;
const session = (client: CognitoClient, storage = new MemoryStorage()) =>
  new LoginSession(client, "caregiver", storage, "test", () => now);

beforeEach(() => { now = NOW; });

test("logins are off without the Cognito settings", () => {
  expect(readConfig({})).toBeNull();
  expect(readConfig({ VITE_COGNITO_REGION: "ap-southeast-1", VITE_COGNITO_CLIENT_ID: "abc" }))
    .toEqual({ region: "ap-southeast-1", clientId: "abc" });
  expect(new LoginSession(null, "caregiver").enabled).toBe(false);
});

test("signing in reads who the caregiver is from the ID token", async () => {
  const { client, calls } = fakeCognito({ "InitiateAuth:USER_PASSWORD_AUTH": [signedIn()] });
  const login = session(client);

  const s = await login.signIn(" sam@example.com ", "pass1234");

  expect(s).toMatchObject({ userId: "G-003", role: "caregiver", email: "sam@example.com", refreshToken: "refresh-1" });
  expect(calls[0].body).toMatchObject({ ClientId: "web", AuthParameters: { USERNAME: "sam@example.com", PASSWORD: "pass1234" } });
  expect(await login.freshIdToken()).toBe(s.idToken);
});

test("a traveller's login can't sign in to the caregiver app", async () => {
  const { client } = fakeCognito({ "InitiateAuth:USER_PASSWORD_AUTH": [
    signedIn(idToken({ "custom:userId": "U-042", "cognito:groups": ["traveller"] }))] });
  const login = session(client);

  await expect(login.signIn("U-042", "code")).rejects.toBeInstanceOf(WrongAccountError);
  expect(login.session).toBeNull();
});

test("an account without its G- ID yet is refused with a clear message", async () => {
  const { client } = fakeCognito({ "InitiateAuth:USER_PASSWORD_AUTH": [signedIn(idToken({ "custom:userId": undefined }))] });

  await expect(session(client).signIn("sam@example.com", "pass1234")).rejects.toThrow("isn't set up yet");
});

test("a reload stays signed in", async () => {
  const storage = new MemoryStorage();
  const { client } = fakeCognito({ "InitiateAuth:USER_PASSWORD_AUTH": [signedIn()] });
  await session(client, storage).signIn("sam@example.com", "pass1234");

  expect(session(client, storage).session?.userId).toBe("G-003");
});

test("the token is refreshed shortly before it runs out, keeping the refresh token", async () => {
  const newer = idToken({}, NOW + 2 * 3_600_000);
  const { client, calls } = fakeCognito({ "InitiateAuth:USER_PASSWORD_AUTH": [signedIn()],
                                          "InitiateAuth:REFRESH_TOKEN_AUTH": [refreshed(newer)] });
  const login = session(client);
  await login.signIn("sam@example.com", "pass1234");

  now = NOW + 50 * 60_000;            // 10 minutes left: still fine
  expect(await login.freshIdToken()).not.toBe(newer);
  now = NOW + 56 * 60_000;            // 4 minutes left: refresh
  expect(await login.freshIdToken()).toBe(newer);

  expect(calls.at(-1)?.body).toMatchObject({ AuthParameters: { REFRESH_TOKEN: "refresh-1" } });
  expect(login.session?.refreshToken).toBe("refresh-1");
});

test("a refresh token that no longer works signs the caregiver out", async () => {
  const { client } = fakeCognito({ "InitiateAuth:USER_PASSWORD_AUTH": [signedIn()],
    "InitiateAuth:REFRESH_TOKEN_AUTH": [{ status: 400, body: { __type: "NotAuthorizedException", message: "Refresh Token has been revoked" } }] });
  const login = session(client);
  await login.signIn("sam@example.com", "pass1234");
  const seen: (string | null)[] = [];
  login.subscribe(() => seen.push(login.idToken()));

  now = NOW + 59 * 60_000;
  expect(await login.freshIdToken()).toBeNull();
  expect(seen).toEqual([null]);
});

test("no network keeps the old token, to try again later", async () => {
  const { client } = fakeCognito({ "InitiateAuth:USER_PASSWORD_AUTH": [signedIn()],
                                   "InitiateAuth:REFRESH_TOKEN_AUTH": [new TypeError("Failed to fetch")] });
  const login = session(client);
  const s = await login.signIn("sam@example.com", "pass1234");

  now = NOW + 59 * 60_000;
  expect(await login.freshIdToken()).toBe(s.idToken);
  expect(login.session).not.toBeNull();
});

test("signing out forgets the session and revokes the refresh token", async () => {
  const storage = new MemoryStorage();
  const { client, calls } = fakeCognito({ "InitiateAuth:USER_PASSWORD_AUTH": [signedIn()] });
  const login = session(client, storage);
  await login.signIn("sam@example.com", "pass1234");

  login.signOut();

  expect(login.session).toBeNull();
  expect(storage.data.size).toBe(0);
  await Promise.resolve();
  expect(calls.at(-1)).toMatchObject({ action: "RevokeToken", body: { Token: "refresh-1" } });
});

test("Cognito's errors are read and explained", async () => {
  const { client } = fakeCognito({ SignUp: [{ status: 400,
    body: { __type: "com.amazonaws#UsernameExistsException", message: "An account with the given email already exists." } }] });

  const error = await client.signUp("uuid", "sam@example.com", "pass1234").catch((e) => e);

  expect(error).toBeInstanceOf(CognitoError);
  expect(error.code).toBe("UsernameExistsException");
  expect(explain(error)).toMatch(/already an account/);
  expect(explain(new CognitoError("NotAuthorizedException", "Incorrect username or password."))).toMatch(/isn't right/);
});
