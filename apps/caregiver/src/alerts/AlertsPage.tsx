// Live alerts for one traveller (issue 5.3). Existing incidents load from GET /api/incidents;
// new ones and every change arrive instantly over GET /api/stream (Server-Sent Events).
import { useCallback, useEffect, useRef, useState } from "react";
import { acknowledge, fetchIncidents, resolve, STREAM_URL } from "./api";
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
const TYPING_PAUSE_MS = 400; // wait for the traveller ID to stop changing before loading
// The Alert Service pings every 15 s. Hearing nothing for this long means the connection is dead even
// if the browser thinks it's open (a proxy can hold it open after the service has gone), so reconnect.
const SILENCE_MS = 40_000;

type Connection = "connecting" | "live" | "reconnecting";

export function AlertsPage({ travellerId, caregiverId }: { travellerId: string; caregiverId: string }) {
  const [incidents, setIncidents] = useState<IncidentMap>({});
  const [connection, setConnection] = useState<Connection>("connecting");
  const [error, setError] = useState<string | null>(null);
  const [showResolved, setShowResolved] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [fresh, setFresh] = useState<ReadonlySet<string>>(new Set());
  const travellerRef = useRef(travellerId);
  travellerRef.current = travellerId;

  const reload = useCallback(async () => {
    const id = travellerRef.current.trim();
    if (!id) return;
    try {
      const loaded = await fetchIncidents(id);
      setIncidents((current) => mergeIncidents(current, loaded));
      setError(null);
    } catch (e) {
      setError(`Couldn't load alerts: ${(e as Error).message}`);
    }
  }, []);

  const highlight = useCallback((incidentId: string) => {
    setFresh((s) => new Set(s).add(incidentId));
    setTimeout(() => setFresh((s) => {
      const next = new Set(s);
      next.delete(incidentId);
      return next;
    }), FRESH_MS);
  }, []);

  // Load the list when the traveller changes (once typing pauses).
  useEffect(() => {
    const timer = setTimeout(() => void reload(), TYPING_PAUSE_MS);
    return () => clearTimeout(timer);
  }, [travellerId, reload]);

  // Live alerts. EventSource reconnects by itself when the connection drops; the silence check below
  // covers a connection that looks open but is dead. After every reconnect the list is reloaded,
  // so anything that happened while disconnected still shows up.
  useEffect(() => {
    let source: EventSource;
    let openedBefore = false;
    let lastHeard = Date.now();

    const connect = () => {
      source = new EventSource(STREAM_URL);
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
        if (event.incident.travellerId !== travellerRef.current.trim()) return;
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
  }, [reload, highlight]);

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

  const mine = Object.values(incidents).filter((i) => i.travellerId === travellerId.trim());
  const shown = sortForDisplay(mine.filter((i) => showResolved || i.state !== "resolved"));
  const openCount = mine.filter((i) => i.state === "open").length;

  return (
    <div className="al">
      <div className="al__bar">
        <h2 className="al__count">
          {openCount === 0 ? "No open alerts" : `${openCount} open alert${openCount > 1 ? "s" : ""}`}
        </h2>
        <label className="al__toggle">
          <input type="checkbox" checked={showResolved} onChange={(e) => setShowResolved(e.target.checked)} />
          Show resolved
        </label>
        <span className={`al__connection al__connection--${connection}`} role="status">
          {connection === "live" ? "● Live" : connection === "connecting" ? "Connecting…" : "Reconnecting…"}
        </span>
      </div>

      {error && (
        <p className="al__error" role="alert">
          {error}
        </p>
      )}

      {shown.length === 0 ? (
        <p className="al__empty">Nothing needs your attention for {travellerId.trim() || "this traveller"}.</p>
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

interface CardProps {
  incident: Incident;
  fresh: boolean;
  busy: boolean;
  onAcknowledge: () => void;
  onResolve: () => void;
}

function AlertCard({ incident: i, fresh, busy, onAcknowledge, onResolve }: CardProps) {
  const name = i.travellerName ?? i.travellerId;
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
        <time dateTime={i.createdAt}>{formatTime(i.createdAt)}</time>
      </div>
      <h3 className="al__title">
        {name}: {problemTitle(i.problemType)}
      </h3>
      {i.disruption?.message && <p className="al__text">{i.disruption.message}</p>}
      {details.length > 0 && <p className="al__details">{details.join(" · ")}</p>}
      <div className="al__actions">
        {i.lastLocation && (
          <a href={`https://www.google.com/maps?q=${i.lastLocation.lat},${i.lastLocation.lng}`} target="_blank" rel="noreferrer">
            Last location
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
