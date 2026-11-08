import { useEffect, useRef } from "react";
import type { RequestQueueState } from "../lib/requests";

/**
 * Shown after pressing Help. The help request is queued and keeps retrying, but if it
 * hasn't reached the server the traveller should not have to wait for it: the Call
 * button is there from the first second, and becomes the main action once we know
 * the request is stuck. If the pack has no phone number yet (Profiles not ready), there
 * is no Call button, and a stuck request tells the traveller to ask someone nearby.
 */
export function HelpSheet({
  askedAt, requests, caregiver, phone, onClose,
}: { askedAt: number; requests: RequestQueueState | null; caregiver: string; phone: string | null; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => ref.current?.focus(), []);

  const stillSending = requests?.pending.some((r) => r.label === "help") ?? true;
  const delivered = !stillSending && requests?.lastDone?.label === "help" && requests.lastDone.at >= askedAt;
  const stuck = stillSending && (requests?.pending.find((r) => r.label === "help")?.attempts ?? 0) > 0;
  const canCall = !!phone;
  const who = caregiver.charAt(0).toUpperCase() + caregiver.slice(1); // "your caregiver" starts a sentence

  return (
    <div className="sheet" role="dialog" aria-modal="true" aria-labelledby="help-title" tabIndex={-1} ref={ref}>
      <div className="sheet__panel">
        <h2 id="help-title" className="sheet__title">
          {delivered ? `${who} has been told` : stuck ? (canCall ? `Call ${caregiver} now` : "Ask someone nearby for help") : `Telling ${caregiver}`}
        </h2>
        <p className="sheet__text">
          {delivered
            ? `Stay where you are. ${caregiver} will help you.`
            : stuck
              ? `We can't reach ${caregiver} through the app right now. We will keep trying.${canCall ? "" : " Show this screen to a staff member or someone near you."}`
              : "Stay where you are."}
        </p>
        {canCall && (
          <a className={`btn ${stuck ? "btn--help" : "btn--secondary"}`} href={`tel:${phone!.replace(/\s/g, "")}`}>
            <PhoneIcon /> Call {caregiver}
          </a>
        )}
        <button className="btn btn--quiet" onClick={onClose}>I'm OK now</button>
      </div>
    </div>
  );
}

function PhoneIcon() {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" aria-hidden fill="currentColor">
      <path d="M6.6 10.8a15.1 15.1 0 0 0 6.6 6.6l2.2-2.2a1 1 0 0 1 1-.25 11.4 11.4 0 0 0 3.6.57 1 1 0 0 1 1 1V20a1 1 0 0 1-1 1A17 17 0 0 1 3 4a1 1 0 0 1 1-1h3.5a1 1 0 0 1 1 1c0 1.25.2 2.45.57 3.57a1 1 0 0 1-.25 1z" />
    </svg>
  );
}
