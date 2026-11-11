// Logins (issue 1.5) in the traveller app: the phone signs in with the traveller ID and setup code, and check-ins
// carry the token. The session logic itself is shared with the caregiver app and tested there.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { CognitoClient } from "../src/auth/cognito";
import { LoginSession } from "../src/auth/session";
import { SignIn } from "../src/auth/SignIn";
import { HttpTransport } from "../src/lib/api";

function jwt(claims: Record<string, unknown>): string {
  const b64 = (o: unknown) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64({ alg: "RS256" })}.${b64(claims)}.sig`;
}

const travellerToken = jwt({ "custom:userId": "U-1A2B3C", "cognito:groups": ["traveller"], exp: 4_000_000_000 });

function cognitoAnswering(idToken: string, seen: Record<string, unknown>[] = []) {
  const fetchImpl = (async (_url: string, init: RequestInit) => {
    seen.push(JSON.parse(String(init.body)));
    return new Response(JSON.stringify({ AuthenticationResult: {
      IdToken: idToken, AccessToken: "a", RefreshToken: "r", ExpiresIn: 3600 } }));
  }) as typeof fetch;
  return new CognitoClient({ region: "ap-southeast-1", clientId: "web" }, fetchImpl);
}

const memory = () => {
  const m = new Map<string, string>();
  return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v),
           removeItem: (k: string) => void m.delete(k) };
};

test("check-ins carry the token, refreshed first if need be", async () => {
  const sent: RequestInit[] = [];
  const transport = new HttpTransport({
    baseUrl: "",
    getToken: async () => "fresh-token",
    fetchImpl: (async (_url: string, init: RequestInit) => {
      sent.push(init);
      return new Response(JSON.stringify({ accepted: 1 }), { status: 202 });
    }) as typeof fetch,
  });

  await transport.post("/api/checkins", {});

  expect((sent[0].headers as Record<string, string>).Authorization).toBe("Bearer fresh-token");
});

test("a caregiver's login can't set up the traveller app", async () => {
  const caregiverToken = jwt({ "custom:userId": "G-003", "cognito:groups": ["caregiver"], exp: 4_000_000_000 });
  const login = new LoginSession(cognitoAnswering(caregiverToken), "traveller", memory());

  await expect(login.signIn("sam@example.com", "pass1234")).rejects.toThrow(/caregiver's login/);
});

test("setting up the phone signs in with the ID and setup code as the caregiver app showed them", async () => {
  const seen: Record<string, unknown>[] = [];
  const login = new LoginSession(cognitoAnswering(travellerToken, seen), "traveller", memory());
  render(<SignIn login={login} travellerId="u-1a2b3c" />);

  fireEvent.change(screen.getByLabelText("Setup code"), { target: { value: " ABCD-2345-EFGH " } });
  fireEvent.click(screen.getByRole("button", { name: "Set up" }));

  await waitFor(() => expect(login.session?.userId).toBe("U-1A2B3C"));
  expect(seen[0]).toMatchObject({ AuthParameters: { USERNAME: "U-1A2B3C", PASSWORD: "abcd-2345-efgh" } });
});

test("a wrong setup code says so in plain words", async () => {
  const fetchImpl = (async () => new Response(JSON.stringify({ __type: "NotAuthorizedException", message: "Incorrect" }),
    { status: 400 })) as typeof fetch;
  const login = new LoginSession(new CognitoClient({ region: "ap-southeast-1", clientId: "web" }, fetchImpl), "traveller", memory());
  render(<SignIn login={login} />);

  fireEvent.change(screen.getByLabelText("Traveller ID"), { target: { value: "U-1A2B3C" } });
  fireEvent.change(screen.getByLabelText("Setup code"), { target: { value: "nope-nope-nope" } });
  fireEvent.click(screen.getByRole("button", { name: "Set up" }));

  expect((await screen.findByRole("alert")).textContent).toMatch(/isn't right/);
  expect(login.session).toBeNull();
});
