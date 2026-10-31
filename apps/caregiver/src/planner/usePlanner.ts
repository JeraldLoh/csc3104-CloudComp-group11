// All the planner's behaviour, with no screen in it: pick a start and destination (by search
// or by clicking the map), plan, choose one of the options, save it as a route. RoutePlanner
// and its parts only read this state and call these actions.
import { useEffect, useRef, useState } from "react";
import type { PlannerApi } from "../journey/api";
import type {
  LatLng, PlaceResult, PlanOption, RouteDraft, RoutePlace, SavedRoute, SortBy, TravelMode,
} from "../journey/types";
import { failure, info, message, type Status } from "./status";

export type Which = "from" | "to";

// Point is a place the user has pinned on the map, with its coordinates and what they typed in the box.
export interface Point extends LatLng {
  label: string;
}
// Focus means the map should be centered on this point, with this zoom, and the n-th time it was set (so a later one can be ignored).
export interface Focus extends LatLng {
  zoom: number;
  n: number;
}

// The planner's state and actions, with no screen in it. The host owns it (so it can refresh the list when
export interface PlannerOptions {
  travellerId: string;
  caregiverId: string;
  /** Start from this trip. With both ends set, it is planned straight away (shareable links). */
  initial?: { from?: Point; to?: Point; mode?: TravelMode };
  onSaved?: (route: SavedRoute) => void;
}

export const PIN_LABEL = "Pinned on map";
const NAMES: Record<Which, string> = { from: "start", to: "destination" };
const FALLBACK_NAMES: Record<Which, string> = { from: "Start", to: "Destination" };
const COORDINATES = /^-?\d+(\.\d+)?\s*,\s*-?\d+(\.\d+)?$/;

// A place's name, or null if it is just coordinates or a pin on the map. The service fills in its own wording for those.
export function placeName(p: Point): string | null {
  const text = p.label.trim();
  return !text || text === PIN_LABEL || COORDINATES.test(text) ? null : text;
}

// The search or plan was superseded by a later one, so its result is ignored. The host can show a message if it wants.
class Superseded extends Error {}

// Type for a pair of values, one for each end of the trip. The planner's state has a pair of points, a pair of queries, and a pair of candidates.
type Pair<T> = Record<Which, T>;

export function usePlanner(api: PlannerApi, opts: PlannerOptions) {
  const { initial } = opts;

  // The refs are the latest truth (async code reads them after an await); the state mirrors
  // them for rendering.
  const [points, setPointsState] = useState<Pair<Point | null>>({ from: initial?.from ?? null, to: initial?.to ?? null });
  const pointsRef = useRef(points);
  const [queries, setQueriesState] = useState<Pair<string>>({ from: initial?.from?.label ?? "", to: initial?.to?.label ?? "" });
  const queriesRef = useRef(queries);
  const commitPoints = (next: Pair<Point | null>) => { pointsRef.current = next; setPointsState(next); };
  const commitQueries = (next: Pair<string>) => { queriesRef.current = next; setQueriesState(next); };

  const [candidates, setCandidates] = useState<Pair<PlaceResult[]>>({ from: [], to: [] });
  const [mode, setMode] = useState<TravelMode>(initial?.mode ?? "any");
  const [sortBy, setSortBy] = useState<SortBy>("walking");
  const [options, setOptions] = useState<PlanOption[]>([]);
  const [activeIndex, setActiveIndex] = useState(0);
  // The ends that were used to make the current options, or null if they have changed since the last plan.
  const [planned, setPlanned] = useState<Pair<Point> | null>(null);
  const [routeName, setRouteName] = useState("");
  const [status, setStatus] = useState<Status>(info(""));
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [focus, setFocus] = useState<Focus | null>(null);

  const planSeq = useRef(0);                      // bumped by every plan and by clear
  const editSeq = useRef<Pair<number>>({ from: 0, to: 0 });   // bumped when that box changes
  const focusCount = useRef(0);

  const moveMapTo = (p: LatLng) => setFocus({ lat: p.lat, lng: p.lng, zoom: 15, n: ++focusCount.current });

  // Set the point for one end of the trip, and update the box to match it. Bumping the editSeq means a search or plan that was started before this change is ignored.
  function setPoint(which: Which, p: Point) {
    editSeq.current[which]++;
    commitPoints({ ...pointsRef.current, [which]: p });
    commitQueries({ ...queriesRef.current, [which]: p.label });
  }

  // Choose one of the search results.
  function choosePlace(which: Which, place: PlaceResult): Point {
    const p = { lat: place.lat, lng: place.lng, label: place.name };
    setPoint(which, p);
    moveMapTo(p);
    return p;
  }

  // Search for places matching the text in one of the boxes, and pin the first result. The host can show a message if it fails or finds nothing.
  async function search(which: Which, text: string): Promise<Point> {
    const q = text.trim();
    if (q.length < 2) throw new Error(`Type a ${NAMES[which]} place (or click the map) first.`);
    const mine = editSeq.current[which];
    const places = await api.searchPlaces(q);
    if (editSeq.current[which] !== mine) throw new Superseded();
    setCandidates((c) => ({ ...c, [which]: places }));
    if (!places.length) throw new Error(`No places found for "${q}".`);
    return choosePlace(which, places[0]);
  }

  // Look up the text in one of the boxes and pin the first result. The host can show a message if it fails or finds nothing.
  async function lookUp(which: Which) {
    setStatus(info("Searching..."));
    try {
      await search(which, queriesRef.current[which]);
      setStatus(info("Pinned the first result. Pick another from the list if it isn't the right one."));
    } catch (e) {
      if (!(e instanceof Superseded)) setStatus(failure(message(e)));
    }
  }

  // Set the text in one of the boxes, and bump the editSeq so a search or plan that was started before this change is ignored. If the text doesn't match the pinned place, clear the pin.
  function setQuery(which: Which, text: string) {
    editSeq.current[which]++;
    commitQueries({ ...queriesRef.current, [which]: text });
    // Editing the text means the pinned place no longer matches the box.
    const p = pointsRef.current[which];
    if (p && text !== p.label) commitPoints({ ...pointsRef.current, [which]: null });
  }

  // Click on the map to pin a place. If both ends are already pinned, clear the pins and start again.
  function clickMap(lat: number, lng: number) {
    const pin = { lat, lng, label: PIN_LABEL };
    const { from, to } = pointsRef.current;
    if (!from) setPoint("from", pin);
    else if (!to) setPoint("to", pin);
    else {
      clear();
      setPoint("from", pin);
    }
  }

  // Clear the planner's state, so the caregiver can start again. The host can show a message if it wants.
  function clear() {
    planSeq.current++;
    editSeq.current = { from: editSeq.current.from + 1, to: editSeq.current.to + 1 };
    commitPoints({ from: null, to: null });
    commitQueries({ from: "", to: "" });
    setCandidates({ from: [], to: [] });
    setOptions([]);
    setActiveIndex(0);
    setPlanned(null);
    setRouteName("");
    setStatus(info(""));
    setBusy(false);
  }

  // Plan a route for the pinned points. The host can show a message if it fails or finds nothing.
  async function plan() {
    const mine = ++planSeq.current;
    const stale = () => planSeq.current !== mine;
    setBusy(true);
    try {
      const ends = {} as Pair<Point>;
      for (const which of ["from", "to"] as const) {
        let p = pointsRef.current[which];
        if (!p) {
          // Text typed but not searched yet: use the first result.
          setStatus(info(`Looking up the ${NAMES[which]}...`));
          p = await search(which, queriesRef.current[which]);
          if (stale()) return;
        }
        ends[which] = p;
      }
      setStatus(info("Planning..."));
      const found = await api.plan({
        from: { lat: ends.from.lat, lng: ends.from.lng },
        to: { lat: ends.to.lat, lng: ends.to.lng },
        mode,
        sortBy,
      });
      if (stale()) return;
      setOptions(found);
      setActiveIndex(0);
      if (!found.length) {
        setPlanned(null);
        setStatus(failure("No route found for that trip."));
        return;
      }
      setPlanned(ends);
      setRouteName(`${placeName(ends.from) ?? FALLBACK_NAMES.from} to ${placeName(ends.to) ?? FALLBACK_NAMES.to}`);
      setStatus(info(`${found.length} option${found.length > 1 ? "s" : ""} found. Click one to see it on the map.`));
    } catch (e) {
      if (!stale() && !(e instanceof Superseded)) setStatus(failure(message(e)));
    } finally {
      if (!stale()) setBusy(false);
    }
  }

  const place = (p: Point): RoutePlace => ({ name: placeName(p), lat: p.lat, lng: p.lng });

  // Save the chosen option as a route. The host can show a message if it fails.
  async function save(): Promise<SavedRoute | null> {
    const option = options[activeIndex];
    if (!option || !planned) {
      setStatus(failure("Plan a route first."));
      return null;
    }
    const travellerId = opts.travellerId.trim();
    const caregiverId = opts.caregiverId.trim();
    if (!travellerId || !caregiverId) {
      setStatus(failure("Choose the traveller and the caregiver first."));
      return null;
    }
    const draft: RouteDraft = {
      travellerId, caregiverId,
      name: routeName.trim() || "Untitled route",
      origin: place(planned.from),
      destination: place(planned.to),
      steps: option.steps,
      path: option.path,
    };
    setSaving(true);
    try {
      const route = await api.createRoute(draft);
      setStatus(info(`Saved as ${route.routeId} (version ${route.version}, not approved yet).`));
      opts.onSaved?.(route);
      return route;
    } catch (e) {
      setStatus(failure(message(e)));
      return null;
    } finally {
      setSaving(false);
    }
  }

  // A shared link with both ends plans straight away.
  const autoPlanned = useRef(false);
  useEffect(() => {
    if (autoPlanned.current) return;
    autoPlanned.current = true;
    if (initial?.from && initial?.to) void plan();
  }, []);

  return {
    points, queries, candidates, mode, sortBy, options, activeIndex, planned, routeName, status, busy, saving, focus,
    activeOption: options[activeIndex] ?? null,
    setQuery, lookUp, choosePlace, clickMap, clear, plan, save,
    setMode, setSortBy, setRouteName,
    selectOption: setActiveIndex,
  };
}

export type Planner = ReturnType<typeof usePlanner>;
