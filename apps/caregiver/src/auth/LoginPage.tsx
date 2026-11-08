// Sign in, sign up (with the emailed code) and reset a forgotten password, for caregivers (issue 1.5).
// A new caregiver gets their G- ID from the user pool once their email is confirmed (careroute-cognito.yaml).
import { useState, type FormEvent } from "react";
import { explain, type CognitoClient } from "./cognito";
import type { LoginSession } from "./session";
import "./login.css";

type Mode = "signin" | "signup" | "confirm" | "forgot" | "reset";

// A sign-up waiting for its emailed code survives a reload (not the password: that is asked for again).
const PENDING = "careroute.signup";
interface Pending { username: string; email: string }
function readPending(): Pending | null {
  try {
    return JSON.parse(sessionStorage.getItem(PENDING) ?? "null");
  } catch {
    return null;
  }
}
function writePending(p: Pending | null) {
  try {
    if (p) sessionStorage.setItem(PENDING, JSON.stringify(p));
    else sessionStorage.removeItem(PENDING);
  } catch { /* fine */ }
}

export function LoginPage({ login }: { login: LoginSession }) {
  const client = login.client as CognitoClient;
  const [pending, setPending] = useState<Pending | null>(readPending);
  const [mode, setMode] = useState<Mode>(pending ? "confirm" : "signin");
  const [email, setEmail] = useState(pending?.email ?? "");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const go = (next: Mode, message: string | null = null) => {
    setMode(next);
    setError(null);
    setNotice(message);
    setCode("");
  };

  async function run(e: FormEvent, action: () => Promise<void>) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      if ((err as { code?: string }).code === "UserNotConfirmedException" && pending) go("confirm");
      setError(explain(err));
    } finally {
      setBusy(false);
    }
  }

  const signIn = (e: FormEvent) => run(e, async () => {
    await login.signIn(email, password);
  });

  const signUp = (e: FormEvent) => run(e, async () => {
    const p = { username: crypto.randomUUID(), email: email.trim() };
    await client.signUp(p.username, p.email, password);
    writePending(p);
    setPending(p);
    go("confirm", `We emailed a code to ${p.email}.`);
  });

  const confirm = (e: FormEvent) => run(e, async () => {
    await client.confirmSignUp(pending!.username, code.trim());
    writePending(null);
    setPending(null);
    if (password) {
      await login.signIn(email, password);   // the account now has its G- ID
    } else {
      go("signin", "Your account is ready. Sign in.");
    }
  });

  const resend = async () => {
    setError(null);
    try {
      await client.resendCode(pending!.username);
      setNotice(`We emailed a new code to ${pending!.email}.`);
    } catch (err) {
      setError(explain(err));
    }
  };

  const forgot = (e: FormEvent) => run(e, async () => {
    await client.forgotPassword(email.trim());
    go("reset", `If there's an account for ${email.trim()}, we emailed it a code.`);
  });

  const reset = (e: FormEvent) => run(e, async () => {
    await client.resetPassword(email.trim(), code.trim(), password);
    await login.signIn(email, password);
  });

  const emailField = (
    <label>Email
      <input type="email" required autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} />
    </label>
  );
  const passwordField = (label: string, autoComplete: string) => (
    <label>{label}
      <input type="password" required minLength={8} autoComplete={autoComplete} value={password}
        onChange={(e) => setPassword(e.target.value)} />
    </label>
  );
  const codeField = (
    <label>Code from the email
      <input inputMode="numeric" required autoComplete="one-time-code" value={code} onChange={(e) => setCode(e.target.value)} />
    </label>
  );

  return (
    <main className="login">
      <div className="login__card">
        <strong className="login__brand">CareRoute caregiver</strong>
        {mode === "signin" && (
          <form onSubmit={signIn}>
            <h1>Sign in</h1>
            {emailField}
            {passwordField("Password", "current-password")}
            <button type="submit" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
            <p className="login__links">
              <button type="button" onClick={() => go("signup")}>Create an account</button>
              <button type="button" onClick={() => go("forgot")}>Forgot password?</button>
            </p>
          </form>
        )}
        {mode === "signup" && (
          <form onSubmit={signUp}>
            <h1>Create an account</h1>
            {emailField}
            {passwordField("Password", "new-password")}
            <small>At least 8 characters, with a lowercase letter and a number.</small>
            <button type="submit" disabled={busy}>{busy ? "Creating…" : "Create account"}</button>
            <p className="login__links">
              <button type="button" onClick={() => go("signin")}>I already have an account</button>
            </p>
          </form>
        )}
        {mode === "confirm" && pending && (
          <form onSubmit={confirm}>
            <h1>Check your email</h1>
            {codeField}
            {!password && <small>After confirming, you'll sign in with your password.</small>}
            <button type="submit" disabled={busy}>{busy ? "Confirming…" : "Confirm"}</button>
            <p className="login__links">
              <button type="button" onClick={() => void resend()}>Send a new code</button>
              <button type="button" onClick={() => { writePending(null); setPending(null); go("signup"); }}>Use another email</button>
            </p>
          </form>
        )}
        {mode === "forgot" && (
          <form onSubmit={forgot}>
            <h1>Reset your password</h1>
            {emailField}
            <button type="submit" disabled={busy}>{busy ? "Sending…" : "Email me a code"}</button>
            <p className="login__links">
              <button type="button" onClick={() => go("signin")}>Back to sign in</button>
            </p>
          </form>
        )}
        {mode === "reset" && (
          <form onSubmit={reset}>
            <h1>Choose a new password</h1>
            {codeField}
            {passwordField("New password", "new-password")}
            <button type="submit" disabled={busy}>{busy ? "Saving…" : "Save and sign in"}</button>
            <p className="login__links">
              <button type="button" onClick={() => go("signin")}>Back to sign in</button>
            </p>
          </form>
        )}
        {notice && !error && <p className="login__notice" role="status">{notice}</p>}
        {error && <p className="login__error" role="alert">{error}</p>}
      </div>
    </main>
  );
}
