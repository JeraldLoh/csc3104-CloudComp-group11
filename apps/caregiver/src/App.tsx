// Stand-in for the caregiver app: a page that hosts the planner until the real dashboard
// (and login, issue 1.5) exists. Everything it does is what a host app will do:
// make an API client, say who the traveller and caregiver are, and give the planner room.
//
// Shareable link: /?from=1.4043,103.9021&to=1.3040,103.8318&mode=bus plans straight away.
import { useMemo, useState } from "react";
import { JourneyApi, RoutePlanner, SavedRoutes, Trips, useSavedRoutes, useTrips } from "./planner";
import type { Point } from "./planner/usePlanner";
import type { TravelMode } from "./journey/types";
import "./app.css";

// Where the traveller app runs; the trip link is built from it. Set VITE_TRAVELLER_APP_URL when deployed.
const TRAVELLER_APP_URL: string = import.meta.env.VITE_TRAVELLER_APP_URL ?? "http://localhost:5173";

function readLink(search = location.search): { from: Point; to: Point; mode?: TravelMode } | null {
  const q = new URLSearchParams(search);
  const pair = (v: string | null) => (v ?? "").split(",").map(Number);
  const [fa, fo] = pair(q.get("from"));
  const [ta, to] = pair(q.get("to"));
  if (![fa, fo, ta, to].every(Number.isFinite)) return null;
  const mode = q.get("mode");
  return {
    from: { lat: fa, lng: fo, label: `${fa}, ${fo}` },
    to: { lat: ta, lng: to, label: `${ta}, ${to}` },
    mode: mode === "bus" || mode === "train" || mode === "any" ? mode : undefined,
  };
}

export default function App() {
  // Dev only: with AUTH_MODE=dev the service believes this header. Real login replaces it.
  const [actAs, setActAs] = useState("G-DEMO");
  const [travellerId, setTravellerId] = useState("U-DEMO");
  const api = useMemo(() => new JourneyApi({ getDevUser: () => actAs }), [actAs]);
  const initial = useMemo(readLink, []) ?? undefined;
  const caregiverId = actAs.startsWith("G-") ? actAs : "G-DEMO";
  const saved = useSavedRoutes(api, travellerId, actAs);
  const trips = useTrips(api, travellerId, actAs);

  return (
    <div className="shell">
      <header className="shell__bar">
        <strong>CareRoute caregiver</strong>
        <label>
          Act as <input value={actAs} onChange={(e) => setActAs(e.target.value)} placeholder="G-003" autoComplete="off" />
        </label>
        <label>
          Traveller <input value={travellerId} onChange={(e) => setTravellerId(e.target.value)} placeholder="U-042" autoComplete="off" />
        </label>
      </header>
      <main className="shell__main">
        <RoutePlanner
          api={api} travellerId={travellerId} caregiverId={caregiverId} initial={initial}
          viewing={saved.opened} onSaved={() => void saved.refresh()}
          sections={[
            { id: "saved", label: "Saved routes", count: saved.routes.length, content: <SavedRoutes saved={saved} trips={trips} /> },
            { id: "trips", label: "Trips", count: trips.trips.length,
              content: <Trips trips={trips} routes={saved.routes} routesLoaded={saved.loaded} travellerAppUrl={TRAVELLER_APP_URL} /> },
          ]}
        />
      </main>
    </div>
  );
}
