// The dashboard's per-traveller numbers: safe locations and caregivers from the Alert Service, the running
// trip from the Journey Service. Each is fetched on its own, so one service being down only blanks its part.
import { useEffect, useRef, useState } from "react";
import type { TripsApi } from "../journey/api";
import { listCaregivers, listSafeLocations } from "./api";
import type { TravellerFacts } from "./overview";
import type { MyTravellers } from "./useMyTravellers";

const REFRESH_MS = 30_000; // alerts are live; trips and other caregivers' changes are picked up this often

async function factsFor(caregiverId: string, travellerId: string,
                        journey: Pick<TripsApi, "listTrips">): Promise<TravellerFacts> {
  const [places, caregivers, trips] = await Promise.allSettled([
    listSafeLocations(caregiverId, travellerId),
    listCaregivers(caregiverId, travellerId),
    journey.listTrips(travellerId),
  ]);
  return {
    safeLocations: places.status === "fulfilled" ? places.value.length : null,
    caregivers: caregivers.status === "fulfilled" ? caregivers.value.length : null,
    activeTrip: trips.status === "fulfilled" ? trips.value.find((t) => t.status === "active") ?? null : null,
    tripsKnown: trips.status === "fulfilled",
  };
}

export function useTravellerFacts(caregiverId: string, my: MyTravellers, journey: Pick<TripsApi, "listTrips">) {
  const [facts, setFacts] = useState<Record<string, TravellerFacts>>({});
  const seq = useRef(0); // an answer for an older request is dropped
  const { reload } = my;
  const idsKey = my.travellers.map((t) => t.travellerId).join(",");

  useEffect(() => {
    const ids = idsKey ? idsKey.split(",") : [];
    const load = async () => {
      const mine = ++seq.current;
      const found = await Promise.all(ids.map(async (id) => [id, await factsFor(caregiverId, id, journey)] as const));
      if (seq.current === mine) setFacts(Object.fromEntries(found));
    };

    void load();
    const timer = setInterval(() => {
      void reload(); // someone may have added or removed a traveller; a change of list loads their facts
      void load();
    }, REFRESH_MS);
    return () => {
      clearInterval(timer);
      seq.current++;
    };
  }, [caregiverId, idsKey, journey, reload]);

  return facts;
}
