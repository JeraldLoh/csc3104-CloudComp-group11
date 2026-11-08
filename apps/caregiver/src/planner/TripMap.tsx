// The Trips tab's map (give it to <RoutePlanner sections> as the trips tab's `stage`): the trip under way with
// where its traveller is, under a header saying which trip it is. Its current route is the steps the traveller
// follows now (a bridging bus sent shows), with where trains aren't running drawn over it. A new route saved for
// the trip (not sent yet) can be switched to and back. Clicking the map changes nothing.
import { useEffect, useMemo, useState } from "react";
import type { SavedRoute, TripLocation } from "../journey/types";
import { MapView, type Traveller } from "./MapView";
import type { Point } from "./usePlanner";
import type { TripsState } from "./useTrips";

/** "just now", "40 s ago", "3 min ago", or the time for anything older than an hour. */
export function ago(iso: string, now = Date.now()): string {
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (s < 10) return "just now";
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  return `at ${new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}

/** The trip's step and when the traveller was last heard from, in a few words. */
export function whereLine(where: TripLocation | undefined): string {
  if (!where) return "Looking for the traveller…";
  const step = `Step ${where.stepIndex + 1} of ${where.stepCount}`;
  return where.at ? `${step} · last check-in ${ago(where.at)}` : `${step} · no check-in yet`;
}

/** What has changed on the trip, if anything, as a short label. */
function changeLabel(trips: TripsState): string | null {
  const change = trips.followed?.override;
  if (change?.status === "pending") return "Bridging bus waiting";
  if (change?.status === "sent") return change.kind === "bridge" ? "On a free bridging bus" : "On a new route";
  return null;
}

export function TripMap({ trips, routes }: { trips: TripsState; routes: SavedRoute[] }) {
  const trip = trips.followed;
  const route = trip ? routes.find((r) => r.routeId === trip.routeId) ?? null : null;
  const where = trip ? trips.locations[trip.tripId] : undefined;
  // The newest route saved for the trip and not sent yet ("Re-route (T-…)", planned on the Plan tab).
  const sentRoute = trip?.override?.status === "sent" ? trip.override.fromRoute : null;
  const reroute = trip ? routes.find((r) => r.forTripId === trip.tripId && r.routeId !== sentRoute) ?? null : null;
  // The traveller's current route: the steps they follow now (with any bridging bus or new route sent), else the
  // saved route until that has loaded. Kept per plan version, so the map isn't refitted each time it is fetched.
  const plan = trip ? trips.plans[trip.tripId] : undefined;
  const planKey = plan ? `${plan.tripId}:${plan.planVersion}` : "";
  const current = useMemo(() => (plan ? { steps: plan.steps, stepPaths: plan.stepPaths, warnings: [] } : null), [planKey]);
  const affectedKey = JSON.stringify(plan?.affected.map((a) => a.path) ?? []);
  const affected = useMemo(() => plan?.affected ?? [], [affectedKey]);
  // With a new route saved for the trip, the caregiver can switch between it and the current route.
  const [showNew, setShowNew] = useState(false);
  useEffect(() => setShowNew(false), [reroute?.routeId, trip?.tripId]);
  const newRoute = showNew ? reroute : null;

  const at = where?.lat != null && where.lng != null ? where : null;
  const traveller: Traveller | null = at && {
    lat: at.lat!, lng: at.lng!, trail: where?.trail ?? [],
    label: where?.source === "checkin" ? "Traveller is here" : "Traveller (no check-in yet)",
    guessed: where?.source !== "checkin",
  };
  const end = route?.destination;
  const to: Point | null = end?.lat != null && end.lng != null ? { lat: end.lat, lng: end.lng, label: end.name ?? "Destination" } : null;
  // Where the step they are on ends, unless that is the destination.
  const step = plan && where ? plan.steps[where.stepIndex] : undefined;
  const next: Point | null = !newRoute && step && where!.stepIndex < plan!.steps.length - 1 && step.to.lat != null && step.to.lng != null
    ? { lat: step.to.lat, lng: step.to.lng, label: step.to.name ?? "Next stop" } : null;
  const change = changeLabel(trips);

  return (
    <>
      <MapView
        from={next} to={to} focus={null} pinLabels={[`Next stop: ${next?.label ?? ""}`, "Destination"]} traveller={traveller}
        insetTop={170} option={newRoute ? null : current} saved={newRoute ?? (current ? null : route)}
        affected={newRoute ? undefined : affected}
      />
      <div className="rp__trip-head" role="status" aria-label="Trip on the map">
        {trip ? (
          <>
            <div className="rp__trip-head-top">
              <span className="rp__pill rp__pill--live"><span className="rp__pulse" aria-hidden="true" />Active trip</span>
              {change && <span className="rp__pill rp__pill--warn">{change}</span>}
              {trips.trips.filter((t) => t.status === "active").length > 1 && (
                <span className="rp__meta">{trip.tripId}</span>
              )}
            </div>
            <div className="rp__trip-head-name">{route?.name ?? trip.routeId}</div>
            <div className="rp__meta">{whereLine(where)}</div>
            {!newRoute && affected.map((a, i) => (
              <div key={i} className="rp__legend"><span className="rp__swatch" aria-hidden="true" />{a.message}</div>
            ))}
            {reroute && (
              <div className="rp__map-switch" role="group" aria-label="Route on the map">
                <button type="button" aria-pressed={!newRoute} onClick={() => setShowNew(false)}>Current route</button>
                <button type="button" aria-pressed={Boolean(newRoute)} onClick={() => setShowNew(true)}
                  title={reroute.name}>New route</button>
              </div>
            )}
          </>
        ) : (
          <>
            <div className="rp__trip-head-name">No trip under way</div>
            <div className="rp__meta">Start a trip and the traveller is followed here.</div>
          </>
        )}
      </div>
    </>
  );
}
