// Signing the phone in (issue 1.5), done once, usually with the caregiver beside the traveller: the traveller ID and
// the setup code the caregiver app showed when it added them. Big fields and plain words, like the rest of the app.
import { useState, type FormEvent } from "react";
import { explain } from "./cognito";
import type { LoginSession } from "./session";

export function SignIn({ login, travellerId = "" }: { login: LoginSession; travellerId?: string }) {
  const [id, setId] = useState(travellerId);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login.signIn(id.trim().toUpperCase(), code.trim().toLowerCase());
    } catch (err) {
      setError(explain(err));
      setBusy(false);
    }
  }

  return (
    <div className="app">
      <main className="app__main">
        <form className="message signin" onSubmit={submit}>
          <h1>Set up this phone</h1>
          <p>Ask your caregiver for your traveller ID and setup code.</p>
          <label>
            Traveller ID
            <input value={id} onChange={(e) => setId(e.target.value)} required placeholder="U-1A2B3C"
              autoCapitalize="characters" autoComplete="username" spellCheck={false} />
          </label>
          <label>
            Setup code
            <input value={code} onChange={(e) => setCode(e.target.value)} required placeholder="abcd-2345-efgh"
              autoCapitalize="none" autoComplete="current-password" spellCheck={false} />
          </label>
          {error && <p className="signin__error" role="alert">{error}</p>}
          <button className="btn btn--primary" type="submit" disabled={busy}>{busy ? "Setting up…" : "Set up"}</button>
        </form>
      </main>
    </div>
  );
}
