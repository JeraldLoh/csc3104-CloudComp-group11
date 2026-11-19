import { useEffect, useState } from "react";
import type { SavedRoutesApi, TripsApi } from "../journey/api";
import type { StepMode } from "../journey/types";

export type PathApi = Pick<TripsApi, "getTrip" | "tripPlan"> & Pick<SavedRoutesApi, "getRoute">;

/** One step's line on the map, coloured by how it is travelled. */
export interface Leg {
  mode: StepMode;
  label: string;
  path: [number, number][];
}

export interface TripPath {
  /** The whole line, start to end. */
  path: [number, number][] | null;
  /** The same line step by step, when the trip's plan says (GET /api/trips/{id}/plan): what the traveller follows
   * now, with a bridging bus or new route in it. Empty when only the saved route's line is known. */
  legs: Leg[];
}

const NONE: TripPath = { path: null, legs: [] };

// The trip's line: its current plan step by step, else the saved route's line; NONE until loaded or when the caller
// may not see it
export function useTripPath(journey: PathApi, tripId: string | undefined): TripPath {
  const [found, setFound] = useState<TripPath>(NONE);

  useEffect(() => {
    setFound(NONE);
    if (!tripId) return;
    let current = true;
    const fromPlan = async (): Promise<TripPath | null> => {
      if (!journey.tripPlan) return null;
      const plan = await journey.tripPlan(tripId);
      const legs = plan.steps
        .map((s, i) => ({ mode: s.mode, label: s.instruction, path: (plan.stepPaths[i] ?? []) as [number, number][] }))
        .filter((leg) => leg.path.length > 0);
      return legs.length ? { path: legs.flatMap((leg) => leg.path), legs } : null;
    };
    const fromRoute = async (): Promise<TripPath> => {
      const trip = await journey.getTrip(tripId);
      const route = await journey.getRoute(trip.routeId);
      return { path: route.path, legs: [] };
    };
    fromPlan()
      .catch(() => null)                               // an older service, or no plan yet: the saved route's line
      .then((plan) => plan ?? fromRoute())
      .then((line) => current && setFound(line))
      // no line is better than a wrong one so a failure just leaves it off
      .catch(() => undefined);
    return () => {
      current = false;
    };
  }, [journey, tripId]);

  return found;
}
