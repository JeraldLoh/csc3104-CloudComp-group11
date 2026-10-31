// Dashboard (issues 5.1 and 5.3): every traveller the logged-in caregiver looks after at a glance, those who
// need attention first. Alerts arrive live; opening a traveller shows their alerts, to acknowledge and resolve.
import { useState } from "react";
import { AlertList, useLiveIncidents } from "../alerts";
import { formatTime, problemTitle } from "../alerts/incidents";
import type { TripsApi } from "../journey/api";
import { buildRows, summarise, type Health, type TravellerRow } from "./overview";
import { Avatar } from "./TravellersPage";
import { isCaregiverId, type MyTravellers } from "./useMyTravellers";
import { useTravellerFacts } from "./useTravellerFacts";
import "./profiles.css";

interface Props {
  caregiverId: string;
  my: MyTravellers;
  journey: Pick<TripsApi, "listTrips">;
  /** Go to the Travellers page, optionally with one traveller open. */
  onOpenProfile: (travellerId?: string) => void;
}

// Mounted with key={caregiverId}: switching caregiver starts from an empty page and a new connection.
export function DashboardPage({ caregiverId, my, journey, onOpenProfile }: Props) {
  const alerts = useLiveIncidents(caregiverId, my);
  const facts = useTravellerFacts(caregiverId, my, journey);
  const [openId, setOpenId] = useState<string | null>(null);

  if (!isCaregiverId(caregiverId)) {
    return (
      <div className="pf">
        <div className="pf__inner">
          <div className="pf__placeholder">
            <p className="pf__placeholder-title">Who's signed in?</p>
            <p>Enter a caregiver ID such as <code>G-001</code> in <strong>Act as</strong>.</p>
          </div>
        </div>
      </div>
    );
  }

  const rows = buildRows(my.travellers, alerts.mine, alerts.known, facts);
  const totals = summarise(rows);
  const problem = alerts.error ?? my.error;
  const open = rows.find((r) => r.traveller.travellerId === openId) ?? null; // gone if they were just removed

  return (
    <div className="pf">
      <div className="pf__inner">
        <header className="pf__head pf__head--split">
          <div>
            <h1 className="pf__h1">Dashboard</h1>
            <p className="pf__sub">Everyone you look after, signed in as <strong>{caregiverId}</strong>.</p>
          </div>
          <span className={`pf__live pf__live--${alerts.connection}`} role="status">
            <span className="pf__dot" aria-hidden="true" />
            {alerts.connection === "live" ? "Live" : alerts.connection === "connecting" ? "Connecting…" : "Reconnecting…"}
          </span>
        </header>

        {problem && <p className="pf__msg pf__msg--error pf__msg--top" role="alert">{problem}</p>}

        {open ? (
          <section>
            <button type="button" className="pf__back" onClick={() => setOpenId(null)}>← All travellers</button>
            <div className="pf__tcard pf__open">
              <div className="pf__tcard-top">
                <Avatar name={open.traveller.name} large />
                <span className="pf__person-text">
                  <h2 className="pf__profile-name">{open.traveller.name}</h2>
                  <span className="pf__muted">{open.traveller.travellerId}</span>
                </span>
                <HealthBadge health={open.health} failed={!!alerts.error} />
              </div>
              <Facts facts={open.facts} />
              <div className="pf__tcard-actions">
                <button type="button" className="pf__btn pf__btn--secondary"
                  onClick={() => onOpenProfile(open.traveller.travellerId)}>
                  Edit profile
                </button>
              </div>
            </div>
            <AlertList caregiverId={caregiverId} incidents={alerts.mine.filter((i) => i.travellerId === openId)}
              known={alerts.known} fresh={alerts.fresh} merge={alerts.merge} onError={alerts.setError} />
          </section>
        ) : my.loaded && my.travellers.length === 0 && !my.error ? (
          <div className="pf__placeholder">
            <p className="pf__placeholder-title">No travellers yet</p>
            <p>Add someone you look after, or ask a caregiver who looks after them to give you access.</p>
            <button type="button" className="pf__btn pf__placeholder-action" onClick={() => onOpenProfile()}>
              Add a traveller
            </button>
          </div>
        ) : my.loaded && (
          <>
            <dl className="pf__tiles">
              <Tile label="Travellers" value={totals.travellers} />
              <Tile label="Need attention" value={totals.needAttention} tone={totals.needAttention ? "attention" : undefined} />
              <Tile label="Urgent alerts" value={totals.urgentAlerts} tone={totals.urgentAlerts ? "urgent" : undefined} />
              <Tile label="On a trip you planned" value={totals.onTrip} />
            </dl>
            <ul className="pf__board">
              {rows.map((row) => (
                <TravellerCard key={row.traveller.travellerId} row={row} failed={!!alerts.error}
                  onOpen={() => setOpenId(row.traveller.travellerId)} />
              ))}
            </ul>
          </>
        )}
      </div>
    </div>
  );
}

function Tile({ label, value, tone }: { label: string; value: number | null; tone?: "attention" | "urgent" }) {
  return (
    <div className={`pf__tile${tone ? ` pf__tile--${tone}` : ""}`}>
      <dt>{label}</dt>
      <dd title={value === null ? "Not known right now" : undefined}>{value ?? "–"}</dd>
    </div>
  );
}

const HEALTH_LABEL: Record<Health, string> = {
  urgent: "Urgent",
  attention: "Needs attention",
  unknown: "Checking…",
  clear: "All clear",
};

function HealthBadge({ health, failed }: { health: Health; failed: boolean }) {
  return (
    <span className={`pf__health pf__health--${health}`}>
      <HealthIcon health={health} />
      {health === "unknown" && failed ? "Can't check alerts" : HEALTH_LABEL[health]}
    </span>
  );
}

function HealthIcon({ health }: { health: Health }) {
  const path = health === "clear" ? "M5 12.5l4.5 4.5L19 7.5"
    : health === "unknown" ? "M12 6v6l4 2M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z"
    : "M12 3.5 2.5 20h19L12 3.5zM12 10v4.5M12 17.5v.01";
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={path} />
    </svg>
  );
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

function tripText(facts: TravellerRow["facts"]): string {
  if (!facts) return "Checking…";
  if (!facts.tripsKnown) return "Can't check trips right now";
  // Only the caregiver who planned a route sees its trips, so "none" can't be said for certain.
  return facts.activeTrip ? `Trip ${facts.activeTrip.tripId} under way` : "No trip of yours under way";
}

function count(value: number | null | undefined, one: string, many: string): string {
  return value === null || value === undefined ? "–" : plural(value, one, many);
}

function Facts({ facts }: { facts: TravellerRow["facts"] }) {
  return (
    <dl className="pf__facts">
      <div>
        <dt>Trip</dt>
        <dd>{tripText(facts)}</dd>
      </div>
      <div>
        <dt>Safe locations</dt>
        <dd>{facts?.safeLocations === 0 ? "None yet" : count(facts?.safeLocations, "place", "places")}</dd>
      </div>
      <div>
        <dt>Caregivers</dt>
        <dd>{count(facts?.caregivers, "caregiver", "caregivers")}</dd>
      </div>
    </dl>
  );
}

function TravellerCard({ row, failed, onOpen }: { row: TravellerRow; failed: boolean; onOpen: () => void }) {
  const { traveller, health, unresolved, facts } = row;
  const top = unresolved[0];

  return (
    <li className={`pf__tcard pf__tcard--${health}`}>
      <button type="button" className="pf__tcard-top pf__tcard-open" onClick={onOpen}>
        <Avatar name={traveller.name} />
        <span className="pf__person-text">
          <strong>{traveller.name}</strong>
          <span className="pf__muted">{traveller.travellerId}</span>
        </span>
        <HealthBadge health={health} failed={failed} />
      </button>

      <div className="pf__tcard-alert">
        {top ? (
          <>
            <p>{problemTitle(top.problemType)}</p>
            <p className="pf__muted">
              <time dateTime={top.createdAt}>{formatTime(top.createdAt)}</time>
              {top.state === "acknowledged" && " · acknowledged"}
              {unresolved.length > 1 && ` · ${unresolved.length - 1} more`}
            </p>
          </>
        ) : (
          <p className="pf__muted">{health === "clear" ? "No open alerts" : "Alerts not known yet"}</p>
        )}
      </div>

      <Facts facts={facts} />

      <div className="pf__tcard-actions">
        <button type="button" className="pf__btn pf__btn--secondary" onClick={onOpen}>
          Open{unresolved.length > 0 ? ` (${plural(unresolved.length, "alert", "alerts")})` : ""}
        </button>
      </div>
    </li>
  );
}
