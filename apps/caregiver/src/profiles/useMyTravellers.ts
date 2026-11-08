// The logged-in caregiver's travellers, shared by the Dashboard and Travellers pages.
import { useCallback, useEffect, useState } from "react";
import { listTravellers, type Traveller } from "./api";

const TYPING_PAUSE_MS = 400; // "Act as" changes on every keystroke; wait until it settles

export interface MyTravellers {
  travellers: Traveller[];
  loaded: boolean;
  error: string | null;
  reload: () => Promise<void>;
}

/** A caregiver ID is "G-" plus letters/digits (contracts/README.md). */
export const isCaregiverId = (id: string) => /^G-[A-Za-z0-9]{1,40}$/.test(id);

export function useMyTravellers(caregiverId: string): MyTravellers {
  const [travellers, setTravellers] = useState<Traveller[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    if (!isCaregiverId(caregiverId)) return;
    try {
      setTravellers(await listTravellers(caregiverId));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoaded(true);
    }
  }, [caregiverId]);

  useEffect(() => {
    // A different caregiver: forget the previous one's travellers straight away.
    setTravellers([]);
    setLoaded(false);
    setError(null);
    const timer = setTimeout(() => void reload(), TYPING_PAUSE_MS);
    return () => clearTimeout(timer);
  }, [reload]);

  return { travellers, loaded, error, reload };
}
