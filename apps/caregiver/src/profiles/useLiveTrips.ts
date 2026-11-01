// Where the caregiver's travellers are now and which step they're on (issue 5.5). Phones check in about every
// 10 s, but straight away on a step change, so this asks often enough for a step to show up quickly.
// `known` is false until an answer arrives, and again if one fails.
import { useEffect, useRef, useState } from "react";
import { fetchLive, type LiveTrip } from "./api";

const REFRESH_MS = 3000;

export function useLiveTrips(caregiverId: string, enabled: boolean) {
  const [live, setLive] = useState<Record<string, LiveTrip>>({});
  const [known, setKnown] = useState(false);
  const seq = useRef(0); // an answer for an older request is dropped

  useEffect(() => {
    if (!enabled) return;
    const load = async () => {
      const mine = ++seq.current;
      try {
        const found = await fetchLive(caregiverId);
        if (seq.current !== mine) return;
        setLive(found);
        setKnown(true);
      } catch {
        if (seq.current !== mine) return;
        setLive({});
        setKnown(false); // the trip lines say so; a bad location is worse than none
      }
    };

    void load();
    const timer = setInterval(() => void load(), REFRESH_MS);
    return () => {
      clearInterval(timer);
      seq.current++;
    };
  }, [caregiverId, enabled]);

  return { live, known };
}
