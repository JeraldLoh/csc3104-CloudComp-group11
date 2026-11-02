// A traveller's trips: make one on an approved route, start or end it, prepare its pack, and
// copy the link the traveller opens on their phone. Give it to <RoutePlanner sections> next to <SavedRoutes>,
// with <TripMap> as the tab's `stage`. Trips under way come first, with where the traveller is.
import { useId, useState } from "react";
import type { DisruptionCheck, SavedRoute, Trip, TripPlan } from "../journey/types";
import { ConfirmButton } from "./ConfirmButton";
import { DisruptionWarnings, ON_THE_STEP } from "./DisruptionWarnings";
import { EasyReadPoints } from "./EasyReadPoints";
import { LandmarkPhotos, StepPhoto } from "./StepPhoto";
import { TripChange } from "./TripChange";
import { whereLine } from "./TripMap";
import { todayLocal, travellerLink, tripProblem, type TripsState } from "./useTrips";
import "./planner.css";

interface Props {
  trips: TripsState;
  // The traveller's approved routes, so a new trip can be made on one of them.
  routes: SavedRoute[];
  // Whether the routes have been loaded yet, so a trip can show a problem if its route was deleted.
  routesLoaded?: boolean;
  // Optional: the URL of the traveller's app, so a link can be shown to open the trip on their phone.
  travellerAppUrl?: string;
  // Optional: the latest disruption check for each route, so a trip can show any warnings.
  disruptions?: Record<string, DisruptionCheck>;
  // Optional: plan a new route for a trip under way on the Plan tab, and open a route on Saved routes.
  onReroute?: (trip: Trip) => void;
  onOpenRoute?: (routeId: string) => void;
}
type RowProps = { trip: Trip; trips: TripsState } & Pick<Props, "routes" | "routesLoaded" | "travellerAppUrl" | "disruptions"
  | "onReroute" | "onOpenRoute">;

// A web link is one that the browser can open, so it can be copied to the clipboard. Other links (e.g. "file://") are not copied, but are shown so the caregiver can type them into a file manager.
const isWebLink = (url: string) => /^https?:\/\//i.test(url);

const routeName = (trip: Trip, routes: SavedRoute[]) => routes.find((r) => r.routeId === trip.routeId)?.name ?? trip.routeId;
/** "Today", or e.g. "Tue 3 Nov". */
const day = (date: string) =>
  date === todayLocal() ? "Today" : new Date(`${date}T00:00`).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
/** The route's disruption warnings now (train faults, bus notices), if any; `trains`: trains aren't running. */
function disruptionOn(check: DisruptionCheck | undefined) {
  const found = check?.warnings.filter((w) => ON_THE_STEP.has(w.type)) ?? [];
  return { any: found.length > 0, trains: found.some((w) => w.type === "train_disruption"), message: found[0]?.message };
}
/** Asked before starting or making a trip on a route with a disruption (nothing when there is none). */
const askFirst = (check: DisruptionCheck | undefined, doing: string, after: string) => {
  const d = disruptionOn(check);
  return d.any ? <><strong>{"This route has a disruption now. "}</strong>{`${d.message} ${doing} anyway? ${after}`}</> : undefined;
};
const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

// The caregiver's trips: make one on an approved route, start or end it, prepare its pack, and copy the link the traveller opens on their phone.
export function Trips({ trips, routes, routesLoaded, travellerAppUrl, disruptions, onReroute, onOpenRoute }: Props) {
  const routeId = useId();
  const dateId = useId();
  // A new trip goes on a route of its own, not on a re-route made for a trip under way.
  const approved = routes.filter((r) => r.approved && !r.forTripId);
  const [chosen, setChosen] = useState("");
  const [date, setDate] = useState(todayLocal());
  // The chosen route may have been edited (so no longer approved) since it was picked.
  const selected = approved.some((r) => r.routeId === chosen) ? chosen : (approved[0]?.routeId ?? "");
  const row = { trips, routes, routesLoaded, travellerAppUrl, disruptions, onReroute, onOpenRoute };
  const active = trips.trips.filter((t) => t.status === "active");
  const planned = trips.trips.filter((t) => t.status === "planned");
  const finished = trips.trips.filter((t) => t.status === "completed");

  return (
    <div className="rp__trips">
      {(trips.status.text || trips.pack) && (
        <div role="status" className={"rp__notice" + (trips.status.error ? " rp__notice--error" : "")}>
          {trips.status.text}
          {trips.pack && (
            <div className="rp__meta">
              {"Pack ready (link valid for an hour): "}
              {isWebLink(trips.pack.packUrl)
                ? <a href={trips.pack.packUrl} target="_blank" rel="noopener noreferrer">{trips.pack.packVersion}</a>
                : trips.pack.packVersion}
            </div>
          )}
        </div>
      )}

      {active.map((t) => <ActiveTrip key={t.tripId} trip={t} {...row} />)}

      <section className="rp__card" aria-label="Make a trip">
        <h2 className="rp__heading">New trip</h2>
        <form onSubmit={(e) => e.preventDefault()}>
          <label htmlFor={routeId}>Route</label>
          <select id={routeId} value={selected} disabled={approved.length === 0} onChange={(e) => setChosen(e.target.value)}>
            {approved.length === 0 && <option value="">No approved route yet</option>}
            {approved.map((r) => <option key={r.routeId} value={r.routeId}>{`${r.name} (${r.routeId})`}</option>)}
          </select>
          <label htmlFor={dateId} className="rp__spaced">Day</label>
          <div className="rp__inline">
            <input id={dateId} type="date" value={date} min={todayLocal()} onChange={(e) => setDate(e.target.value)} />
            <ConfirmButton className="rp__btn" disabled={trips.creating || approved.length === 0} yes="Yes, make it"
              confirm={askFirst(disruptions?.[selected], "Make the trip",
                "You can then switch it to the free bridging bus or plan a new route under Coming up.")}
              onConfirm={() => void trips.create(selected, date)}>
              Make trip
            </ConfirmButton>
          </div>
        </form>
      </section>

      <section className="rp__card" aria-label="Trips">
        <h2 className="rp__heading">Coming up</h2>
        {!trips.traveller ? (
          <div className="rp__meta">Choose a traveller to see their trips.</div>
        ) : trips.loading && trips.trips.length === 0 ? (
          <div className="rp__meta">Loading...</div>
        ) : planned.length === 0 ? (
          <div className="rp__meta">{trips.trips.length === 0 ? "No trips yet. Approve a route, then make a trip." : "Nothing planned."}</div>
        ) : (
          <ul className="rp__list">{planned.map((t) => <PlannedTrip key={t.tripId} trip={t} {...row} />)}</ul>
        )}
      </section>

      {finished.length > 0 && (
        <details className="rp__card rp__finished">
          <summary className="rp__heading">Finished <span className="rp__count">{finished.length}</span></summary>
          <ul className="rp__list">
            {finished.map((t) => (
              <li key={t.tripId} className="rp__trip-row">
                <span className="rp__trip-text">
                  <span className="rp__name">{routeName(t, routes)}</span>
                  <span className="rp__meta">{`${day(t.date)}${t.endedAt ? `, ended ${clock(t.endedAt)}` : ""} · ${t.tripId}`}</span>
                </span>
                <span className="rp__pill">Done</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

/** A trip under way: where the traveller is, its controls, and changing it. */
function ActiveTrip({ trip, trips, routes, routesLoaded, travellerAppUrl, disruptions, onReroute, onOpenRoute }: RowProps) {
  const problem = tripProblem(trip, routes, routesLoaded);
  const busy = trips.busyId === trip.tripId;
  const where = trips.locations[trip.tripId];
  const plan = trips.plans[trip.tripId];
  const onMap = trips.followed?.tripId === trip.tripId;
  const link = travellerAppUrl && !problem ? travellerLink(travellerAppUrl, trip) : null;
  return (
    <section className={"rp__card rp__live-card" + (onMap ? " rp__live-card--shown" : "")} aria-label={`Trip ${trip.tripId}, under way`}>
      <div className="rp__live-top">
        <span className="rp__pill rp__pill--live"><span className="rp__pulse" aria-hidden="true" />Under way</span>
        <span className="rp__meta">{trip.startedAt ? `since ${clock(trip.startedAt)}` : day(trip.date)}</span>
        <button type="button" className="rp__btn rp__btn--secondary rp__btn--small rp__push" aria-pressed={onMap}
          onClick={() => trips.follow(trip.tripId)}>
          {onMap ? "On the map" : "Show on the map"}
        </button>
      </div>
      <div className="rp__trip-title">{routeName(trip, routes)}</div>
      <div className="rp__meta">{trip.tripId}</div>

      {plan && plan.steps.length > 0 ? (
        <StepBrowser key={`${trip.tripId}:${plan.planVersion}`} plan={plan} current={where?.stepIndex ?? trip.currentStepIndex ?? 0} />
      ) : where && where.stepCount > 0 && where.step && (
        <div className="rp__now">
          <span className={`rp__chip rp__chip--${where.step.mode}`}>{where.step.mode}</span>
          <span>{where.step.instruction}</span>
        </div>
      )}
      <div className={where?.source === "checkin" ? "rp__live" : "rp__meta"}>{whereLine(where)}</div>

      {problem && <div className="rp__warning">{problem}</div>}
      {!problem && <DisruptionWarnings check={disruptions?.[trip.routeId]} />}
      {!problem && (
        <TripChange trip={trip} trips={trips} reroutes={routes.filter((r) => r.forTripId === trip.tripId)}
          onReroute={onReroute} onOpenRoute={onOpenRoute} trainsDown={disruptionOn(disruptions?.[trip.routeId]).trains} />
      )}

      <div className="rp__live-actions">
        {!problem && (
          <button type="button" className="rp__btn rp__btn--secondary rp__btn--small" disabled={busy}
            aria-label={`Prepare pack for ${trip.tripId}`} onClick={() => void trips.preparePack(trip.tripId)}>Prepare pack</button>
        )}
        <button type="button" className="rp__btn rp__btn--danger rp__btn--small rp__push" disabled={busy}
          aria-label={`End ${trip.tripId}`} onClick={() => void trips.end(trip.tripId)}>End trip</button>
      </div>
      {link && (
        <details className="rp__details">
          <summary>Traveller's phone link</summary>
          <TravellerLink url={link} tripId={trip.tripId} />
        </details>
      )}
    </section>
  );
}

/** The trip's steps one at a time: where the traveller is, unless the caregiver looks at another (the bar's
 *  segments, or the arrows); "Back to their step" follows the traveller again. */
function StepBrowser({ plan, current }: { plan: TripPlan; current: number }) {
  const [picked, setPicked] = useState<number | null>(null);           // null: follow the traveller
  const last = plan.steps.length - 1;
  const here = Math.min(current, last);
  const shown = Math.min(picked ?? here, last);
  const step = plan.steps[shown];
  const show = (i: number) => setPicked(i === here ? null : Math.max(0, Math.min(last, i)));
  const broken = plan.affected.find((a) => a.stepIndex === shown);
  return (
    <div className="rp__browse">
      <ol className="rp__progress rp__progress--pick" aria-label="Steps">
        {plan.steps.map((s, i) => (
          <li key={i} className={(i < here ? "done" : i === here ? "now" : "") + (i === shown ? " shown" : "")
            + (plan.affected.some((a) => a.stepIndex === i) ? " broken" : "")}>
            <button type="button" aria-label={`Step ${i + 1}: ${s.instruction}`} aria-current={i === shown ? "step" : undefined}
              onClick={() => show(i)} />
          </li>
        ))}
      </ol>
      <div className="rp__browse-nav">
        <button type="button" className="rp__btn rp__btn--secondary rp__btn--small" aria-label="Previous step"
          disabled={shown === 0} onClick={() => show(shown - 1)}>‹</button>
        <span className="rp__meta">
          {`Step ${shown + 1} of ${plan.steps.length}`}{shown === here ? " · the traveller is here" : shown < here ? " · done" : ""}
        </span>
        <button type="button" className="rp__btn rp__btn--secondary rp__btn--small" aria-label="Next step"
          disabled={shown === last} onClick={() => show(shown + 1)}>›</button>
        {shown !== here && (
          <button type="button" className="rp__link-btn" onClick={() => setPicked(null)}>Back to their step</button>
        )}
      </div>
      <div className="rp__now">
        <span className={`rp__chip rp__chip--${step.mode}`}>{step.mode}</span>
        <span>{step.instruction}</span>
      </div>
      {broken && <div className="rp__legend"><span className="rp__swatch" aria-hidden="true" />{broken.message}</div>}
      <EasyReadPoints text={step.easyRead} label="What the traveller reads" />
      <StepPhoto url={step.photoUrl} alt={`Step ${shown + 1}: where it ends`} fallback={null} />
      {step.landmarks && step.landmarks.length > 0 && <LandmarkPhotos landmarks={step.landmarks} />}
    </div>
  );
}

/** A trip not started yet. */
function PlannedTrip({ trip, trips, routes, routesLoaded, travellerAppUrl, disruptions, onReroute, onOpenRoute }: RowProps) {
  const problem = tripProblem(trip, routes, routesLoaded);
  // Once the trip has been changed (a bridging bus, a new route), the route's disruption no longer needs asking about.
  const check = trip.override?.status === "sent" ? undefined : disruptions?.[trip.routeId];
  const busy = trips.busyId === trip.tripId;
  const link = travellerAppUrl && !problem ? travellerLink(travellerAppUrl, trip) : null;
  return (
    <li className="rp__trip">
      <div className="rp__trip-row">
        <span className="rp__trip-text">
          <span className="rp__name">{routeName(trip, routes)}</span>
          <span className="rp__meta">{`${day(trip.date)} · ${trip.tripId}`}</span>
        </span>
        {!problem && (
          <>
            <button type="button" className="rp__btn rp__btn--secondary rp__btn--small" disabled={busy}
              aria-label={`Prepare pack for ${trip.tripId}`} onClick={() => void trips.preparePack(trip.tripId)}>Pack</button>
            <ConfirmButton className="rp__btn rp__btn--small" disabled={busy} aria-label={`Start ${trip.tripId}`} yes="Yes, start it"
              confirm={askFirst(check, "Start the trip", "The free bridging bus or a new route can still be chosen below.")}
              onConfirm={() => void trips.start(trip.tripId)}>
              Start
            </ConfirmButton>
          </>
        )}
      </div>
      {problem && <div className="rp__warning">{problem}</div>}
      {!problem && <DisruptionWarnings check={disruptions?.[trip.routeId]} />}
      {!problem && (
        <TripChange trip={trip} trips={trips} reroutes={routes.filter((r) => r.forTripId === trip.tripId)}
          onReroute={onReroute} onOpenRoute={onOpenRoute} trainsDown={disruptionOn(check).trains} />
      )}
      {link && (
        <details className="rp__details">
          <summary>Traveller's phone link</summary>
          <TravellerLink url={link} tripId={trip.tripId} />
        </details>
      )}
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
