// Live alerts for the logged-in caregiver's travellers (issues 5.1 and 5.3). Existing incidents load from
// GET /api/incidents; new ones and every change arrive instantly over GET /api/stream (Server-Sent Events).
// The Alert Service only lets a caregiver see their own travellers, so this page can't show anyone else's.
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { isCaregiverId, type MyTravellers } from "../profiles";
import { acknowledge, fetchIncidents, resolve, streamUrl } from "./api";
import {
  applyEvent,
  mergeIncidents,
  problemTitle,
  sortForDisplay,
  type AlertEvent,
  type Incident,
  type IncidentMap,
} from "./incidents";
import "./alerts.css";

const FRESH_MS = 4000; // how long a new alert stays highlighted
// The Alert Service pings every 15 s. Hearing nothing for this long means the connection is dead even
// if the browser thinks it's open (a proxy can hold it open after the service has gone), so reconnect.
const SILENCE_MS = 40_000;

type Connection = "connecting" | "live" | "reconnecting";

// Mounted with key={caregiverId}: switching caregiver starts from an empty page and a new connection.
export function AlertsPage({ caregiverId, my }: { caregiverId: string; my: MyTravellers }) {
  const [incidents, setIncidents] = useState<IncidentMap>({});
  const [connection, setConnection] = useState<Connection>("connecting");
  const [error, setError] = useState<string | null>(null);
  const [showResolved, setShowResolved] = useState(false);
  const [only, setOnly] = useState<string>(""); // "" = all my travellers
  const [busy, setBusy] = useState<string | null>(null);
  const [fresh, setFresh] = useState<ReadonlySet<string>>(new Set());
  const loggedIn = isCaregiverId(caregiverId);

  const travellerIds = my.travellers.map((t) => t.travellerId);
  const idsKey = travellerIds.join(",");
  const idsRef = useRef(travellerIds);
  idsRef.current = travellerIds;
  const names = Object.fromEntries(my.travellers.map((t) => [t.travellerId, t.name]));

  const reload = useCallback(async () => {
    try {
      const lists = await Promise.all(idsRef.current.map((id) => fetchIncidents(id, caregiverId)));
      setIncidents((current) => mergeIncidents(current, lists.flat()));
      setError(null);
    } catch (e) {
      setError(`Couldn't load alerts: ${(e as Error).message}`);
    }
  }, [caregiverId]);

  const highlight = useCallback((incidentId: string) => {
    setFresh((s) => new Set(s).add(incidentId));
    setTimeout(() => setFresh((s) => {
      const next = new Set(s);
      next.delete(incidentId);
      return next;
    }), FRESH_MS);
  }, []);

  // Load alerts whenever the list of travellers changes (e.g. one was just added).
  useEffect(() => {
    if (loggedIn) void reload();
  }, [idsKey, loggedIn, reload]);

  // Live alerts. EventSource reconnects by itself when the connection drops; the silence check below
  // covers a connection that looks open but is dead. After every reconnect the list is reloaded,
  // so anything that happened while disconnected still shows up.
  useEffect(() => {
    if (!loggedIn) return;
    let source: EventSource;
    let openedBefore = false;
    let lastHeard = Date.now();

    const connect = () => {
      source = new EventSource(streamUrl(caregiverId));
      source.onopen = () => {
        lastHeard = Date.now();
        setConnection("live");
        if (openedBefore) void reload();
        openedBefore = true;
      };
      source.onerror = () => setConnection("reconnecting");
      source.addEventListener("ping", () => {
        lastHeard = Date.now();
      });
      source.onmessage = (msg) => {
        lastHeard = Date.now();
        const event: AlertEvent = JSON.parse(msg.data);
        // The service already sends only this caregiver's travellers; this also covers AUTH_MODE=off.
        if (!idsRef.current.includes(event.incident.travellerId)) return;
        setIncidents((current) => applyEvent(current, event));
        if (event.event === "incident.opened" || event.event === "incident.reopened") {
          highlight(event.incident.incidentId);
        }
      };
    };

    connect();
    const silenceCheck = setInterval(() => {
      if (Date.now() - lastHeard < SILENCE_MS) return;
      source.close();
      setConnection("reconnecting");
      lastHeard = Date.now();
      connect();
    }, 5000);

    return () => {
      clearInterval(silenceCheck);
      source.close();
    };
  }, [caregiverId, loggedIn, reload, highlight]);

  async function act(action: typeof acknowledge, incidentId: string) {
    setBusy(incidentId);
    try {
      const updated = await action(incidentId, caregiverId);
      setIncidents((current) => mergeIncidents(current, [updated]));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  if (!loggedIn) {
    return (
      <div className="al">
        <div className="al__inner">
          <Empty title="Who's signed in?">
            Enter a caregiver ID such as <code>G-001</code> in <strong>Act as</strong>.
          </Empty>
        </div>
      </div>
    );
  }

  const mine = Object.values(incidents).filter(
    (i) => travellerIds.includes(i.travellerId) && (!only || i.travellerId === only),
  );
  const shown = sortForDisplay(mine.filter((i) => showResolved || i.state !== "resolved"));
  const count = (test: (i: Incident) => boolean) => mine.filter(test).length;

  return (
    <div className="al">
      <div className="al__inner">
        <header className="al__head">
          <div>
            <h1 className="al__h1">Alerts</h1>
            <p className="al__sub">Live updates for the travellers you look after</p>
          </div>
          <span className={`al__live al__live--${connection}`} role="status">
            <span className="al__dot" aria-hidden="true" />
            {connection === "live" ? "Live" : connection === "connecting" ? "Connecting…" : "Reconnecting…"}
          </span>
        </header>

        <div className="al__toolbar">
          <div className="al__stats">
            <Stat label="Open" value={count((i) => i.state === "open")} tone="open" />
            <Stat label="Urgent" value={count((i) => i.state !== "resolved" && i.severity === "urgent")} tone="urgent" />
            <Stat label="Acknowledged" value={count((i) => i.state === "acknowledged")} tone="ack" />
          </div>
          <div className="al__filters">
            {my.travellers.length > 1 && (
              <select value={only} onChange={(e) => setOnly(e.target.value)} aria-label="Which traveller">
                <option value="">All travellers</option>
                {my.travellers.map((t) => <option key={t.travellerId} value={t.travellerId}>{t.name}</option>)}
              </select>
            )}
            <label className="al__toggle">
              <input type="checkbox" checked={showResolved} onChange={(e) => setShowResolved(e.target.checked)} />
              Show resolved
            </label>
          </div>
        </div>

        {(error || my.error) && (
          <p className="al__error" role="alert">
            {error ?? my.error}
          </p>
        )}

        {my.loaded && my.travellers.length === 0 ? (
          <Empty title="No travellers yet">
            Add a traveller in the <strong>Travellers</strong> tab, or ask a caregiver who looks after them to add you.
          </Empty>
        ) : shown.length === 0 ? (
          <Empty title="All clear">Nothing needs your attention right now.</Empty>
        ) : (
          <ul className="al__cards">
            {shown.map((incident) => (
              <AlertCard
                key={incident.incidentId}
                incident={incident}
                name={names[incident.travellerId] ?? incident.travellerName ?? incident.travellerId}
                fresh={fresh.has(incident.incidentId)}
                busy={busy === incident.incidentId}
                onAcknowledge={() => act(acknowledge, incident.incidentId)}
                onResolve={() => act(resolve, incident.incidentId)}
              />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone: "open" | "urgent" | "ack" }) {
  return (
    <div className={`al__stat al__stat--${tone}`}>
      <span className="al__stat-value">{value}</span>
      <span className="al__stat-label">{label}</span>
    </div>
  );
}

function Empty({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="al__empty">
      <p className="al__empty-title">{title}</p>
      <p>{children}</p>
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
  name: string;
  fresh: boolean;
  busy: boolean;
  onAcknowledge: () => void;
  onResolve: () => void;
}

function AlertCard({ incident: i, name, fresh, busy, onAcknowledge, onResolve }: CardProps) {
  const urgent = i.severity === "urgent" && i.state !== "resolved";
  const details = [
    i.tripId && `Trip ${i.tripId}`,
    i.stepIndex != null && `step ${i.stepIndex}`,
    i.detectionCount && i.detectionCount > 1 && `detected ${i.detectionCount}×`,
    i.acknowledgedBy && i.state !== "open" && `acknowledged by ${i.acknowledgedBy}`,
  ].filter(Boolean);

  return (
    <li className={`al__card al__card--${i.state}${urgent ? " al__card--urgent" : ""}${fresh ? " al__card--fresh" : ""}`}>
      <div className="al__card-top">
        <span className={`al__badge al__badge--${urgent ? "urgent" : i.state}`}>{urgent ? "Urgent" : i.state}</span>
        <time className="al__time" dateTime={i.createdAt}>{formatTime(i.createdAt)}</time>
      </div>
      <h3 className="al__title">
        <span className="al__name">{name}</span> · {problemTitle(i.problemType)}
      </h3>
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

function formatTime(iso: string): string {
  const d = new Date(iso);
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
