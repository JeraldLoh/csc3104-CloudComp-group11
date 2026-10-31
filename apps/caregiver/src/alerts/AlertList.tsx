// One traveller's alerts (issues 5.1 and 5.3), shown on the dashboard when a traveller is opened. They stay
// up to date through useLiveIncidents; a caregiver acknowledges and resolves them here.
import { useState } from "react";
import { acknowledge, resolve } from "./api";
import { formatTime, problemTitle, sortForDisplay, type Incident } from "./incidents";
import "./alerts.css";

interface Props {
  caregiverId: string;
  incidents: Incident[];
  /** False while they're loading or if they couldn't be: no alerts doesn't mean all clear then. */
  known: boolean;
  fresh: ReadonlySet<string>;
  /** Keeps an alert the service just changed. */
  merge: (updated: Incident) => void;
  onError: (message: string) => void;
}

export function AlertList({ caregiverId, incidents, known, fresh, merge, onError }: Props) {
  const [showResolved, setShowResolved] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  async function act(action: typeof acknowledge, incidentId: string) {
    setBusy(incidentId);
    try {
      merge(await action(incidentId, caregiverId));
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const shown = sortForDisplay(incidents.filter((i) => showResolved || i.state !== "resolved"));

  return (
    <div className="al al--embedded">
      <div className="al__toolbar">
        <h3 className="al__list-title">Alerts</h3>
        <label className="al__toggle">
          <input type="checkbox" checked={showResolved} onChange={(e) => setShowResolved(e.target.checked)} />
          Show resolved
        </label>
      </div>
      {shown.length === 0 ? (
        <div className="al__empty">
          {known ? (
            <>
              <p className="al__empty-title">All clear</p>
              <p>Nothing needs your attention right now.</p>
            </>
          ) : (
            <p className="al__empty-title">Alerts not known yet</p>
          )}
        </div>
      ) : (
        <ul className="al__cards">
          {shown.map((incident) => (
            <AlertCard
              key={incident.incidentId}
              incident={incident}
              fresh={fresh.has(incident.incidentId)}
              busy={busy === incident.incidentId}
              onAcknowledge={() => act(acknowledge, incident.incidentId)}
              onResolve={() => act(resolve, incident.incidentId)}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function PinIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 21s-7-6.1-7-11a7 7 0 0 1 14 0c0 4.9-7 11-7 11z" />
      <circle cx="12" cy="10" r="2.5" />
    </svg>
  );
}

interface CardProps {
  incident: Incident;
  fresh: boolean;
  busy: boolean;
  onAcknowledge: () => void;
  onResolve: () => void;
}

function AlertCard({ incident: i, fresh, busy, onAcknowledge, onResolve }: CardProps) {
  const urgent = i.severity === "urgent" && i.state !== "resolved";
  const details = [
    i.tripId && `Trip ${i.tripId}`,
    i.stepIndex != null && `step ${i.stepIndex + 1}`, // counted from 0; the traveller's phone says "Step 1 of 4"
    i.detectionCount && i.detectionCount > 1 && `detected ${i.detectionCount}×`,
    i.acknowledgedBy && i.state !== "open" && `acknowledged by ${i.acknowledgedBy}`,
  ].filter(Boolean);

  return (
    <li className={`al__card al__card--${i.state}${urgent ? " al__card--urgent" : ""}${fresh ? " al__card--fresh" : ""}`}>
      <div className="al__card-top">
        <span className={`al__badge al__badge--${urgent ? "urgent" : i.state}`}>{urgent ? "Urgent" : i.state}</span>
        <time className="al__time" dateTime={i.createdAt}>{formatTime(i.createdAt)}</time>
      </div>
      <h3 className="al__title">{problemTitle(i.problemType)}</h3>
      {i.disruption?.message && <p className="al__text">{i.disruption.message}</p>}
      {details.length > 0 && <p className="al__details">{details.join(" · ")}</p>}
      <div className="al__actions">
        {i.lastLocation && (
          <a href={`https://www.google.com/maps?q=${i.lastLocation.lat},${i.lastLocation.lng}`} target="_blank" rel="noreferrer">
            <PinIcon /> Last known location
          </a>
        )}
        {i.state === "open" && (
          <button type="button" className="al__btn" onClick={onAcknowledge} disabled={busy}>
            Acknowledge
          </button>
        )}
        {i.state !== "resolved" && (
          <button type="button" className="al__btn al__btn--secondary" onClick={onResolve} disabled={busy}>
            Resolve
          </button>
        )}
      </div>
    </li>
  );
}
