// Which trip this phone is following. Until login exists (issue 1.5), the caregiver
// shares a link like  https://<app>/?trip=T-001&traveller=U-042  and the phone remembers it.
import type { Session } from "../types";

const KEY = "careroute.session";
const TRIP = /^T-[A-Za-z0-9-]+$/;
const TRAVELLER = /^U-[A-Za-z0-9-]+$/;

export interface Flags {
  demo: boolean;
  debug: boolean;
}

export function readFlags(search = location.search): Flags {
  const q = new URLSearchParams(search);
  return { demo: q.get("demo") === "1", debug: q.get("debug") === "1" };
}

export function readSession(search = location.search): Session | null {
  const q = new URLSearchParams(search);
  const tripId = q.get("trip");
  const travellerId = q.get("traveller");
  if (tripId && travellerId && TRIP.test(tripId) && TRAVELLER.test(travellerId)) {
    const s = { tripId, travellerId };
    try {
      localStorage.setItem(KEY, JSON.stringify(s));
    } catch { /* private mode: fine, the link still works */ }
    return s;
  }
  if (q.get("demo") === "1") return { tripId: "T-001", travellerId: "U-042" };
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) ?? "null") as Session | null;
    if (saved && TRIP.test(saved.tripId) && TRAVELLER.test(saved.travellerId)) return saved;
  } catch { /* ignore */ }
  return null;
}
