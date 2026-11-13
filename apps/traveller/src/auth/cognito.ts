// The Cognito user pool's public API (issue 1.5), called straight from the browser: no SDK, just the few
// actions the login pages need. Cognito allows these calls from any web page (CORS) and they need no AWS keys,
// only the app client's ID. The pool: cloudformation/careroute-cognito.yaml.
// The same file is in both apps (apps/caregiver and apps/traveller); keep them the same.

export interface CognitoConfig {
  region: string;
  clientId: string;
}

/** Set VITE_COGNITO_REGION and VITE_COGNITO_CLIENT_ID (the careroute-cognito stack's outputs) to turn logins on.
 * Without them the app keeps its local stand-in (no login). */
export function readConfig(env: Record<string, string | undefined> = import.meta.env): CognitoConfig | null {
  const region = env.VITE_COGNITO_REGION?.trim();
  const clientId = env.VITE_COGNITO_CLIENT_ID?.trim();
  return region && clientId ? { region, clientId } : null;
}

/** Cognito's error, e.g. code "NotAuthorizedException" (wrong password) or "UserNotConfirmedException". */
export class CognitoError extends Error {
  constructor(readonly code: string, message: string) {
    super(message);
  }
}

export interface Tokens {
  IdToken: string;
  AccessToken: string;
  RefreshToken?: string;   // only on sign-in, not on refresh
  ExpiresIn: number;
}

export class CognitoClient {
  constructor(readonly config: CognitoConfig, private fetchImpl: typeof fetch = (...a) => fetch(...a)) {}

  private async call<T>(action: string, body: Record<string, unknown>): Promise<T> {
    let res: Response;
    try {
      res = await this.fetchImpl(`https://cognito-idp.${this.config.region}.amazonaws.com/`, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-amz-json-1.1",
          "X-Amz-Target": `AWSCognitoIdentityProviderService.${action}`,
        },
        body: JSON.stringify({ ClientId: this.config.clientId, ...body }),
      });
    } catch {
      throw new CognitoError("NetworkError", "Can't reach the login service. Check the connection and try again.");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const code = String(data.__type ?? "UnknownError").split("#").pop()!;
      throw new CognitoError(code, data.message ?? `Login service error (${res.status})`);
    }
    return data as T;
  }

  async signIn(username: string, password: string): Promise<Tokens> {
    const r = await this.call<{ AuthenticationResult?: Tokens; ChallengeName?: string }>("InitiateAuth", {
      AuthFlow: "USER_PASSWORD_AUTH",
      AuthParameters: { USERNAME: username, PASSWORD: password },
    });
    if (!r.AuthenticationResult) {
      // Accounts are made so this never happens (no temporary passwords, no MFA)
      throw new CognitoError(r.ChallengeName ?? "Challenge", "This account needs a step the app doesn't support.");
    }
    return r.AuthenticationResult;
  }

  async refresh(refreshToken: string): Promise<Tokens> {
    const r = await this.call<{ AuthenticationResult: Tokens }>("InitiateAuth", {
      AuthFlow: "REFRESH_TOKEN_AUTH",
      AuthParameters: { REFRESH_TOKEN: refreshToken },
    });
    return r.AuthenticationResult;
  }

  /** Signs the refresh token out, so a copy of it stops working. */
  async revoke(refreshToken: string): Promise<void> {
    await this.call("RevokeToken", { Token: refreshToken });
  }

  /** `username`: a fresh random one (the pool signs people in by email, which can't also be the username). */
  async signUp(username: string, email: string, password: string): Promise<void> {
    await this.call("SignUp", { Username: username, Password: password, UserAttributes: [{ Name: "email", Value: email }] });
  }

  async confirmSignUp(username: string, code: string): Promise<void> {
    await this.call("ConfirmSignUp", { Username: username, ConfirmationCode: code });
  }

  async resendCode(username: string): Promise<void> {
    await this.call("ResendConfirmationCode", { Username: username });
  }

  async forgotPassword(email: string): Promise<void> {
    await this.call("ForgotPassword", { Username: email });
  }

  async resetPassword(email: string, code: string, password: string): Promise<void> {
    await this.call("ConfirmForgotPassword", { Username: email, ConfirmationCode: code, Password: password });
  }
}

/** The claims of a JWT, unchecked: the services verify tokens; the app only reads who it is for. */
export function claimsOf(jwt: string): Record<string, unknown> {
  const part = jwt.split(".")[1] ?? "";
  const base64 = part.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(part.length / 4) * 4, "=");
  try {
    const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    return {};
  }
}

/** Wording for the login pages. */
export function explain(e: unknown): string {
  if (!(e instanceof CognitoError)) return e instanceof Error ? e.message : "Something went wrong";
  switch (e.code) {
    case "NotAuthorizedException":
    case "UserNotFoundException":
      return "That login or password isn't right.";
    case "UsernameExistsException":
      return "There's already an account with that email. Sign in instead.";
    case "CodeMismatchException":
      return "That code isn't right. Check the email and try again.";
    case "ExpiredCodeException":
      return "That code has expired. Ask for a new one.";
    case "InvalidPasswordException":
      return "The password needs at least 8 characters, with a lowercase letter and a number.";
    case "LimitExceededException":
    case "TooManyRequestsException":
      return "Too many tries. Wait a few minutes and try again.";
    default:
      return e.message;
  }
}
