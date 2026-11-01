// planned route line for the trip on the map
import { useEffect, useState } from "react";
import type { SavedRoutesApi, TripsApi } from "../journey/api";

export type PathApi = Pick<TripsApi, "getTrip"> & Pick<SavedRoutesApi, "getRoute">;

// path of the trip route or null until loaded or when the caller may not see it
export function useTripPath(journey: PathApi, tripId: string | undefined) {
  const [path, setPath] = useState<[number, number][] | null>(null);

  useEffect(() => {
    setPath(null);
    if (!tripId) return;
    let current = true;
    journey.getTrip(tripId)
      .then((trip) => journey.getRoute(trip.routeId))
      .then((route) => current && setPath(route.path))
      // no line is better than a wrong one so a failure just leaves it off
      .catch(() => undefined);
    return () => {
      current = false;
    };
  }, [journey, tripId]);

  return path;
}
