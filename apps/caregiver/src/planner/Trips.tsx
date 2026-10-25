// A traveller's trips: make one on an approved route, start or end it, prepare its pack, and
// copy the link the traveller opens on their phone. Give it to <RoutePlanner sections> next to <SavedRoutes>.
import { useId, useState } from "react";
import type { SavedRoute, Trip } from "../journey/types";
import { todayLocal, travellerLink, tripProblem, type TripsState } from "./useTrips";
import "./planner.css";

interface Props {
  trips: TripsState;
  /** The traveller's routes (from useSavedRoutes). Only approved ones can have a trip. */
  routes: SavedRoute[];
  /** True once `routes` is the traveller's whole list (useSavedRoutes `loaded`), so a trip whose route is missing is flagged. */
  routesLoaded?: boolean;
  /** Where the traveller app is hosted, e.g. "https://app.example.com". Without it no link is shown. */
  travellerAppUrl?: string;
}

/** Only web links from the service are opened; anything else (javascript:, data:) is shown as text. */
const isWebLink = (url: string) => /^https?:\/\//i.test(url);

export function Trips({ trips, routes, routesLoaded, travellerAppUrl }: Props) {
  const routeId = useId();
  const dateId = useId();
  const approved = routes.filter((r) => r.approved);
  const [chosen, setChosen] = useState("");
  const [date, setDate] = useState(todayLocal());
  // The chosen route may have been edited (so no longer approved) since it was picked.
  const selected = approved.some((r) => r.routeId === chosen) ? chosen : (approved[0]?.routeId ?? "");

  return (
    <div className="rp__trips">
      <section className="rp__card" aria-label="Make a trip">
        <h2 className="rp__heading">New trip</h2>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void trips.create(selected, date);
          }}
        >
          <label htmlFor={routeId}>Make a trip on</label>
          <select id={routeId} value={selected} disabled={approved.length === 0} onChange={(e) => setChosen(e.target.value)}>
            {approved.length === 0 && <option value="">No approved route yet</option>}
            {approved.map((r) => <option key={r.routeId} value={r.routeId}>{`${r.name} (${r.routeId})`}</option>)}
          </select>
          <label htmlFor={dateId} className="rp__spaced">Day</label>
          <input id={dateId} type="date" value={date} min={todayLocal()} onChange={(e) => setDate(e.target.value)} />
          <button type="submit" className="rp__btn rp__btn--spaced" disabled={trips.creating || approved.length === 0}>New trip</button>
        </form>
      </section>

      <section className="rp__card" aria-label="Trips">
        <h2 className="rp__heading">Trips</h2>

        {!trips.traveller ? (
          <div className="rp__meta">Choose a traveller to see their trips.</div>
        ) : trips.loading && trips.trips.length === 0 ? (
          <div className="rp__meta">Loading...</div>
        ) : trips.trips.length === 0 ? (
          <div className="rp__meta">No trips yet. Approve a route, then make a trip.</div>
        ) : (
          <ul className="rp__list">
            {trips.trips.map((t) => (
              <TripRow key={t.tripId} trip={t} trips={trips} routes={routes} routesLoaded={routesLoaded} travellerAppUrl={travellerAppUrl} />
            ))}
          </ul>
        )}

        {trips.status.text && (
          <div role="status" className={"rp__status" + (trips.status.error ? " rp__status--error" : "")}>{trips.status.text}</div>
        )}
        {trips.pack && (
          <div className="rp__meta">
            {"Pack ready (link valid for an hour): "}
            {isWebLink(trips.pack.packUrl)
              ? <a href={trips.pack.packUrl} target="_blank" rel="noopener noreferrer">{trips.pack.packVersion}</a>
              : trips.pack.packVersion}
          </div>
        )}
      </section>
    </div>
  );
}

function TripRow({ trip, trips, routes, routesLoaded, travellerAppUrl }: { trip: Trip; trips: TripsState } & Pick<Props, "routes" | "routesLoaded" | "travellerAppUrl">) {
  const problem = tripProblem(trip, routes, routesLoaded);
  const busy = trips.busyId === trip.tripId;
  const link = travellerAppUrl && trip.status !== "completed" && !problem ? travellerLink(travellerAppUrl, trip) : null;
  return (
    <li className="rp__trip">
      <div className="rp__item">
        <span className="rp__trip-text">
          {`${trip.date} `}
          <span className="rp__meta">{`${trip.tripId}, ${trip.routeId} v${trip.routeVersion}`}</span>
          <span className={"rp__badge" + (trip.status === "completed" ? "" : " rp__badge--ok")}>{trip.status}</span>
        </span>
        {trip.status === "planned" && !problem && (
          <button type="button" className="rp__btn rp__btn--small" disabled={busy} aria-label={`Start ${trip.tripId}`} onClick={() => void trips.start(trip.tripId)}>Start</button>
        )}
        {trip.status === "active" && (
          <button type="button" className="rp__btn rp__btn--small" disabled={busy} aria-label={`End ${trip.tripId}`} onClick={() => void trips.end(trip.tripId)}>End</button>
        )}
        {trip.status !== "completed" && !problem && (
          <button type="button" className="rp__btn rp__btn--secondary rp__btn--small" disabled={busy} aria-label={`Prepare pack for ${trip.tripId}`} onClick={() => void trips.preparePack(trip.tripId)}>Pack</button>
        )}
      </div>
      {problem && <div className="rp__warning">{problem}</div>}
      {link && <TravellerLink url={link} tripId={trip.tripId} />}
    </li>
  );
}

function TravellerLink({ url, tripId }: { url: string; tripId: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="rp__link">
      <input readOnly value={url} aria-label={`Traveller link for ${tripId}`} onFocus={(e) => e.currentTarget.select()} />
      <button
        type="button"
        className="rp__btn rp__btn--secondary rp__btn--small"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(url);
            setCopied(true);
          } catch { /* no clipboard (http page, old browser): the box is selectable */ }
        }}
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}
