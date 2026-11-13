// The caregiver app: a page that hosts the planner and the caregiver pages. It makes an API client, says who the
// traveller and caregiver are, and gives the planner room.
//
// Who the caregiver is: with logins on (VITE_COGNITO_* set, issue 1.5) whoever signed in, and every request carries
// their token; without them, the "Act as" box (the services' AUTH_MODE=dev), for local demos.
//
// Shareable link: /?from=1.4043,103.9021&to=1.3040,103.8318&mode=bus plans straight away.
import { useEffect, useMemo, useState } from "react";
import { login, LoginPage, useLogin, type Session } from "./auth";
import { DashboardPage, TravellersPage, useMyTravellers } from "./profiles";
import { JourneyApi, RoutePlanner, SavedRoutes, TripMap, Trips, tripProblem, useDisruptions, useSavedRoutes, useTrips } from "./planner";
import type { Point } from "./planner/usePlanner";
import type { DataModeInfo, DataSource, RouteStep, TravelMode, Trip } from "./journey/types";
import "./app.css";

// Local demos only: switch the service between saved and live data. Hidden when the service can't switch
// (DEMO_UI off, e.g. on AWS), so the real app never shows it.
function DataSwitch({ api }: { api: JourneyApi }) {
  const [info, setInfo] = useState<DataModeInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.getDataMode().then(setInfo, () => setInfo(null));
  }, [api]);
  if (!info) return null;

  const source: DataSource = info.onemap === "recorded" && info.lta === "recorded" ? "recorded" : "live";
  const change = async (next: DataSource) => {
    setBusy(true);
    setError(null);
    try {
      setInfo(await api.setDataMode(next));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't switch");
    } finally {
      setBusy(false);
    }
  };

  return (
    <label title="Saved = the API answers recorded by capture_data.py (no keys needed). Live = the real OneMap and LTA.">
      Data
      <select value={source} disabled={busy} onChange={(e) => void change(e.target.value as DataSource)}>
        <option value="recorded">Saved</option>
        <option value="live">Live</option>
      </select>
      {source === "recorded" && info.onemapRecordedAt && <small>saved on {info.onemapRecordedAt}</small>}
      {error && <small role="alert">{error}</small>}
    </label>
  );
}

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

// The pages. #dashboard or #travellers in the address opens that page directly, and #dashboard/U-042 opens that
// traveller (a push notification's link). Alerts are on the dashboard now, so the old #alerts link
// (services/demo-control uses it) goes there.
type Page = "plan" | "dashboard" | "travellers";
const pageFromHash = (): Page => {
  const page = location.hash.slice(1).split("/")[0];
  return page === "dashboard" || page === "alerts" ? "dashboard" : page === "travellers" ? page : "plan";
};
const travellerFromHash = (): string | null => location.hash.slice(1).split("/")[1] || null;

export default function App() {
  const session = useLogin();
  if (login.enabled && !session) return <LoginPage login={login} />;
  // A new caregiver signing in starts from a clean page
  return <Shell key={session?.userId ?? "dev"} signedIn={session} />;
}

function Shell({ signedIn }: { signedIn: Session | null }) {
  const [page, setPage] = useState<Page>(pageFromHash);
  const [focusId, setFocusId] = useState<string | null>(travellerFromHash); // the traveller a link or button was about
  // A new object each time a link asks, so the dashboard opens the traveller again even if it's the same one.
  const [linked, setLinked] = useState(() => ({ travellerId: travellerFromHash() }));
  useEffect(() => {
    const follow = () => {
      setPage(pageFromHash());
      setFocusId(travellerFromHash());
      setLinked({ travellerId: travellerFromHash() });
    };
    // A tapped notification, with the app already open: the push worker says where to go.
    const fromPush = (e: MessageEvent) => {
      if (e.data?.type !== "careroute-open") return;
      const url = new URL(e.data.url);
      if (url.origin !== location.origin) return;
      history.replaceState(null, "", url.pathname + url.search + url.hash);
      follow();
    };
    window.addEventListener("hashchange", follow);
    navigator.serviceWorker?.addEventListener("message", fromPush);
    navigator.serviceWorker?.startMessages(); // deliver messages now, not only once the page has loaded
    return () => {
      window.removeEventListener("hashchange", follow);
      navigator.serviceWorker?.removeEventListener("message", fromPush);
    };
  }, []);
  const openPage = (next: Page, travellerId: string | null = null) => {
    setPage(next);
    setFocusId(travellerId);
    history.replaceState(null, "", next === "plan" ? location.pathname + location.search : `#${next}`);
  };
  // Without logins, "Act as" says who is calling: with AUTH_MODE=dev the services believe this header.
  const [actAs, setActAs] = useState(signedIn?.userId ?? "G-DEMO");
  const [travellerId, setTravellerId] = useState("U-DEMO");
  const api = useMemo(() => signedIn ? new JourneyApi({ getToken: login.freshIdToken })
    : new JourneyApi({ getDevUser: () => actAs }), [actAs, signedIn]);
  const initial = useMemo(readLink, []) ?? undefined;
  const caregiverId = actAs.startsWith("G-") ? actAs : "G-DEMO";
  const saved = useSavedRoutes(api, travellerId, actAs);
  const trips = useTrips(api, travellerId, actAs);
  // A new route for a trip under way is planned on the Plan tab, from where the traveller is to the trip's destination.
  const [reroute, setReroute] = useState<{ tripId: string; from: Point; to: Point } | null>(null);
  const [goTo, setGoTo] = useState<{ tab: string } | null>(null);
  async function planReroute(trip: Trip) {
    const end = saved.routes.find((r) => r.routeId === trip.routeId)?.destination;
    const start = saved.routes.find((r) => r.routeId === trip.routeId)?.origin;
    const from = trip.status === "planned" && start ? { ...start, label: start.name }
      : (trips.locations[trip.tripId] ?? await api.tripLocation(trip.tripId).catch(() => null))?.replanFrom;
    if (from?.lat == null || from.lng == null || end?.lat == null || end.lng == null) return;
    setReroute({ tripId: trip.tripId,
      from: { lat: from.lat, lng: from.lng, label: trip.status === "planned" ? from.label ?? "Start" : "Where the traveller is" },
      to: { lat: end.lat, lng: end.lng, label: end.name ?? "Destination" } });
  }
  function openRoute(routeId: string) {
    void saved.open(routeId);
    setGoTo({ tab: "saved" });
  }

  // Re-check against LTA, every minute: the opened saved route and the route of every trip that hasn't finished.
  const watched: Record<string, RouteStep[]> = {};
  for (const t of trips.trips) {
    const route = saved.routes.find((r) => r.routeId === t.routeId);
    if (route && t.status !== "completed" && !tripProblem(t, saved.routes, saved.loaded)) watched[route.routeId] = route.steps;
  }
  if (saved.opened) watched[saved.opened.routeId] = saved.opened.steps;
  for (const r of saved.routes) if (r.approved && !r.forTripId) watched[r.routeId] = r.steps;   // for "New trip"
  const disruptions = useDisruptions(api, watched);
  // The "Act as" caregiver's own travellers (Alert Service, issue 5.1), for the Dashboard and Travellers pages.
  const me = actAs.trim();
  const myTravellers = useMyTravellers(me);

  return (
    <div className="shell">
      <header className="shell__bar">
        <strong>CareRoute caregiver</strong>
        <nav className="shell__tabs">
          <button type="button" aria-pressed={page === "plan"} onClick={() => openPage("plan")}>Plan routes</button>
          <button type="button" aria-pressed={page === "dashboard"} onClick={() => openPage("dashboard")}>Dashboard</button>
          <button type="button" aria-pressed={page === "travellers"} onClick={() => openPage("travellers")}>Travellers</button>
        </nav>
        {signedIn ? (
          <span className="shell__me" title={signedIn.userId}>
            {signedIn.email ?? signedIn.userId}
            <button type="button" onClick={() => login.signOut()}>Sign out</button>
          </span>
        ) : (
          <label>
            Act as <input value={actAs} onChange={(e) => setActAs(e.target.value)} placeholder="G-003" autoComplete="off" />
          </label>
        )}
        <label>
          Traveller <input value={travellerId} onChange={(e) => setTravellerId(e.target.value)} placeholder="U-042" autoComplete="off" />
        </label>
        <DataSwitch api={api} />
      </header>
      <main className="shell__main">
        {page === "dashboard" ? <DashboardPage key={me} caregiverId={me} my={myTravellers} journey={api} linked={linked}
            onOpenProfile={(travellerId) => openPage("travellers", travellerId)} />
          : page === "travellers" ? <TravellersPage key={me} caregiverId={me} my={myTravellers} initialTravellerId={focusId} />
          : <RoutePlanner
          api={api} travellerId={travellerId} caregiverId={caregiverId} initial={initial}
          viewing={saved.opened} reroute={reroute} goTo={goTo} onSaved={() => void saved.refresh()}
          sections={[
            { id: "saved", label: "Saved routes", count: saved.routes.length,
              content: <SavedRoutes saved={saved} trips={trips} disruptions={disruptions} /> },
            { id: "trips", label: "Trips", count: trips.trips.length,
              content: <Trips trips={trips} routes={saved.routes} routesLoaded={saved.loaded} travellerAppUrl={TRAVELLER_APP_URL}
                disruptions={disruptions} onReroute={(t) => void planReroute(t)} onOpenRoute={openRoute} />,
              stage: <TripMap trips={trips} routes={saved.routes} /> },
          ]}
        />}
      </main>
    </div>
  );
}
