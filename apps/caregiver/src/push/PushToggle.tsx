// "Alerts on this computer" on the dashboard (issue 5.4). Hidden when push isn't set up at all.
import { useEffect, useState } from "react";
import { pushState, turnOnPush, type PushState } from "./push";

const TEXT: Partial<Record<PushState, string>> = {
  on: "Urgent alerts on this computer: on",
  blocked: "Notifications are blocked for this site in your browser settings",
  "server-off": "Saved, but the Alert Service can't send notifications yet",
  failed: "Couldn't turn on notifications. Try again in a moment.",
  unsupported: "This browser can't show notifications",
};

export function PushToggle({ caregiverId }: { caregiverId: string }) {
  const [state, setState] = useState<PushState | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let current = true;
    void pushState().then(async (found) => {
      // Already allowed: register again, since a browser's token can change.
      const now = found === "on" ? await turnOnPush(caregiverId) : found;
      if (current) setState(now);
    });
    return () => {
      current = false;
    };
  }, [caregiverId]);

  if (!state || state === "off") return null;
  if (state === "ask" || state === "failed") {
    return (
      <span className="pf__push">
        <button type="button" className="pf__btn pf__btn--secondary" disabled={busy} onClick={async () => {
          setBusy(true);
          setState(await turnOnPush(caregiverId));
          setBusy(false);
        }}>
          {busy ? "Turning on…" : "Get urgent alerts on this computer"}
        </button>
        {state === "failed" && <span className="pf__muted">{TEXT.failed}</span>}
      </span>
    );
  }
  return <span className={`pf__push pf__push--${state}`} role="status">{TEXT[state]}</span>;
}
