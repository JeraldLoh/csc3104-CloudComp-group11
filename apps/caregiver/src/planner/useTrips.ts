// A traveller's trips: the list, making a trip on an approved route, starting and ending it,
// and preparing its offline pack. Like the other hooks it has no screen in it.
//
// A trip is one journey on one day, and it is pinned to the route version it was made with:
// if the route is edited afterwards, the service refuses to start the trip or build its pack
// ("approve the new version and create a new trip"). `tripProblem` spots that early.
import { useEffect, useRef, useState } from "react";
import type { TripsApi } from "../journey/api";
import type { PackLink, SavedRoute, Trip, TripLocation, TripPlan } from "../journey/types";
import { failure, info, message, type Status } from "./status";

/** How often trips are looked at again while one is under way. */
const TRIP_POLL_MS = 15_000;
/** How often the travellers of trips under way are looked for (their phones check in about every 10 s). */
const LOCATION_POLL_MS = 5_000;
/** How often the plans of trips under way are looked at again (a breakdown can start or end); also when one changes. */
const PLAN_POLL_MS = 30_000;

// Today as YYYY-MM-DD in the browser's timezone (the day the caregiver means).
export function todayLocal(now = new Date()): string {
  const two = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${two(now.getMonth() + 1)}-${two(now.getDate())}`;
}

// The trip's problem, if any, that prevents it from being started or having its pack built.
export function tripProblem(trip: Trip, routes: SavedRoute[], routesLoaded = false): string | null {
  if (trip.status === "completed") return null;
  const route = routes.find((r) => r.routeId === trip.routeId);
  if (!route) return routesLoaded ? "The route was deleted after this trip was made. Make a new trip on another route." : null;
  if (route.version !== trip.routeVersion || !route.approved) {
    return "The route was changed after this trip was made. Approve it, then make a new trip.";
  }
  return null;
}

// The link the traveller opens on their phone to see the trip, if the caregiver knows where the app is hosted.
export function travellerLink(appUrl: string, trip: Trip): string {
  const base = appUrl.replace(/\/+$/, "");
  return `${base}/?trip=${encodeURIComponent(trip.tripId)}&traveller=${encodeURIComponent(trip.travellerId)}`;
}

// The traveller's trips, with actions to make one on an approved route, start or end it, and prepare its offline pack.
export function useTrips(api: TripsApi, travellerId: string, reloadKey?: unknown) {
  const traveller = travellerId.trim();
  const latestApi = useRef(api);
  latestApi.current = api;
  const [trips, setTrips] = useState<Trip[]>([]);
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<Status>(info(""));
  /** The trip with a request running (create is not tied to a trip, so it has its own flag). */
  const [busyId, setBusyId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [pack, setPack] = useState<(PackLink & { tripId: string }) | null>(null);
  /** The trip under way the map follows (the first one, until the caregiver picks another). */
  const [following, setFollowing] = useState<string | null>(null);
  /** Where the traveller of each trip under way is, by trip id. */
  const [locations, setLocations] = useState<Record<string, TripLocation>>({});
  /** The steps each trip under way's traveller follows now, by trip id. */
  const [plans, setPlans] = useState<Record<string, TripPlan>>({});

  const listSeq = useRef(0);   // an answer for an older request (or another traveller) is dropped

  async function refresh() {
    const mine = ++listSeq.current;
    if (!traveller) {
      setTrips([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const found = await latestApi.current.listTrips(traveller);
      if (listSeq.current === mine) setTrips(found);
    } catch (e) {
      if (listSeq.current === mine) setStatus(failure(message(e)));
    } finally {
      if (listSeq.current === mine) setLoading(false);
    }
  }

  useEffect(() => {
    setTrips([]);
    setPack(null);
    setFollowing(null);
    setLocations({});
    setPlans({});
    setStatus(info(""));
    void refresh();
  }, [traveller, reloadKey]);

  // While a trip is under way, look again every TRIP_POLL_MS, so a bridging bus proposed during a breakdown (and
  // its countdown, and it being sent) shows without a reload.
  const anyActive = trips.some((t) => t.status === "active");
  useEffect(() => {
    if (!anyActive) return;
    const timer = setInterval(() => void refresh(), TRIP_POLL_MS);
    return () => clearInterval(timer);
  }, [anyActive, traveller, reloadKey]);

  const active = trips.filter((t) => t.status === "active");
  const followed = active.find((t) => t.tripId === following) ?? active[0] ?? null;
  /** Follow this trip on the map. */
  const follow = (tripId: string) => setFollowing(tripId);

  // Where each traveller under way is, every LOCATION_POLL_MS. One that can't be found keeps its last answer.
  const activeIds = active.map((t) => t.tripId).join(",");
  useEffect(() => {
    const ids = activeIds ? activeIds.split(",") : [];
    setLocations((all) => Object.fromEntries(Object.entries(all).filter(([id]) => ids.includes(id))));
    if (!ids.length || !latestApi.current.tripLocation) return;
    let stopped = false;
    const look = () => ids.forEach((id) => {
      latestApi.current.tripLocation!(id).then(
        (where) => { if (!stopped) setLocations((all) => ({ ...all, [id]: where })); },
        () => { /* the trip just ended, or the service is busy: try again next time */ },
      );
    });
    look();
    const timer = setInterval(look, LOCATION_POLL_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [activeIds, reloadKey]);

  // Each trip under way's plan (its steps for the card, its lines for the map): again when one changes (a bridging
  // bus or new route sent) and every PLAN_POLL_MS.
  const planKey = active.map((t) => `${t.tripId}:${t.planVersion}`).join(",");
  useEffect(() => {
    const ids = active.map((t) => t.tripId);
    setPlans((all) => Object.fromEntries(Object.entries(all).filter(([id]) => ids.includes(id))));
    if (!ids.length || !latestApi.current.tripPlan) return;
    let stopped = false;
    const look = () => ids.forEach((id) => latestApi.current.tripPlan!(id).then(
      (plan) => { if (!stopped) setPlans((all) => ({ ...all, [id]: plan })); },
      () => { /* the trip just ended, or the service is busy: the saved route is drawn meanwhile */ },
    ));
    look();
    const timer = setInterval(look, PLAN_POLL_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [planKey, reloadKey]);

  /** Makes a trip on an approved route for `date` (YYYY-MM-DD). */
  async function create(routeId: string, date: string): Promise<Trip | null> {
    if (!routeId) {
      setStatus(failure("Choose an approved route first."));
      return null;
    }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
      setStatus(failure("Choose the day of the trip."));
      return null;
    }
    setCreating(true);
    try {
      const trip = await latestApi.current.createTrip(routeId, date);
      setStatus(info(`Trip ${trip.tripId} planned for ${trip.date} on route ${trip.routeId} (version ${trip.routeVersion}).`));
      await refresh();
      return trip;
    } catch (e) {
      setStatus(failure(message(e)));
      return null;
    } finally {
      setCreating(false);
    }
  }

  async function change(tripId: string, call: (api: TripsApi) => Promise<Trip>, done: (t: Trip) => string) {
    setBusyId(tripId);
    try {
      const trip = await call(latestApi.current);
      setTrips((ts) => ts.map((t) => (t.tripId === trip.tripId ? trip : t)));
      setStatus(info(done(trip)));
    } catch (e) {
      setStatus(failure(message(e)));
    } finally {
      setBusyId(null);
    }
  }

  const start = (tripId: string) => change(tripId, (a) => a.startTrip(tripId), (t) => `Trip ${t.tripId} is now ${t.status}.`);
  const end = (tripId: string) => change(tripId, (a) => a.endTrip(tripId), (t) => `Trip ${t.tripId} is now ${t.status}.`);

  const sendBridge = (tripId: string) => change(tripId, (a) => a.sendBridge!(tripId),
    () => "The bridging bus was sent. The traveller's app shows the new steps.");
  const dismissBridge = (tripId: string) => change(tripId, (a) => a.dismissBridge!(tripId),
    () => "The bridging bus won't be sent. The traveller keeps their planned steps (you can still send it).");
  /** The steps applyBridge would give the trip, to show before choosing it (throws if there is no free bus). */
  const previewBridge = (tripId: string) => latestApi.current.previewBridge!(tripId);
  const applyBridge = (tripId: string) => change(tripId, (a) => a.applyBridge!(tripId),
    (t) => t.status === "active" ? "The bridging bus was sent. The traveller's app shows the new steps."
      : "The trip now uses the free bridging bus past the breakdown.");

  /** Send a route saved for the trip (approved on Saved routes) to its traveller. */
  const sendRoute = (tripId: string, route: SavedRoute) => change(tripId, (a) => a.sendRoute!(tripId, route.routeId),
    () => `${route.name} was sent. The traveller's app shows the new steps, with its wording and photos.`);

  /** Builds the trip's offline pack and shows the (temporary) link to it. */
  async function preparePack(tripId: string) {
    setBusyId(tripId);
    try {
      const link = await latestApi.current.tripPack(tripId);
      setPack({ ...link, tripId });
      setStatus(info(`Route pack ${link.packVersion} stored. The link is valid for an hour.`));
    } catch (e) {
      setStatus(failure(message(e)));
    } finally {
      setBusyId(null);
    }
  }

  return { traveller, trips, loading, status, busyId, creating, pack, refresh, create, start, end, preparePack,
    sendBridge, dismissBridge, previewBridge, applyBridge, sendRoute, followed, follow, locations, plans };
}

export type TripsState = ReturnType<typeof useTrips>;
