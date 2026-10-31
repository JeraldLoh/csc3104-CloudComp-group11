// A traveller's saved routes: the list, opening one to look at it on the map, editing it, and approving.
// Like usePlanner it has no screen in it. The host owns it (so it can refresh the list when
// the planner saves a route) and hands the result to <SavedRoutes> and <RoutePlanner viewing>.
import { useEffect, useRef, useState } from "react";
import { ApiError, type SavedRoutesApi } from "../journey/api";
import type { EasyReadSuggestion, Landmark, RouteUpdate, SavedRoute, UploadedPhoto } from "../journey/types";
import { failure, info, message, type Status } from "./status";

/**
 * What the editor can change. `easyRead[i]`: undefined keeps step i as it is, null clears it (the service fills in
 * its own wording). `photoKey[i]`: undefined keeps the photo, a key (from uploadPhoto) replaces it, null clears it
 * (the service adds a Street View photo again when there is one). `landmarks[i]`: undefined keeps them, a list
 * replaces them (e.g. the AI's, with their photos).
 */
export interface RouteChanges {
  name: string;
  easyRead: (string | null | undefined)[];
  photoKey?: (string | null | undefined)[];
  landmarks?: (Landmark[] | undefined)[];
}

/** AI wording for the open route, waiting in the editor for the caregiver to check. */
export interface PendingSuggestion extends EasyReadSuggestion {
  routeId: string;
}

/**
 * `reloadKey`: change it to fetch again for the same traveller, e.g. when the login changes.
 * (Changing `api` does not: a host that makes a new client on every render would loop forever.)
 */
export function useSavedRoutes(api: SavedRoutesApi, travellerId: string, reloadKey?: unknown) {
  const traveller = travellerId.trim();
  const latestApi = useRef(api);
  latestApi.current = api;
  const [routes, setRoutes] = useState<SavedRoute[]>([]);
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<Status>(info(""));
  /** The route being looked at on the map, fetched fresh when it was opened. */
  const [opened, setOpened] = useState<SavedRoute | null>(null);
  /** The route being approved right now. */
  const [approvingId, setApprovingId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  /** The route being deleted right now. */
  const [deletingId, setDeletingId] = useState<string | null>(null);
  /** The list has been fetched for this traveller, so a route missing from it really is gone. */
  const [loaded, setLoaded] = useState(false);
  /** Someone saved the open route since it was opened, so saving over it was refused. Reload it. */
  const [outOfDate, setOutOfDate] = useState(false);
  /** The route the AI is rewording right now, and its answer once it comes (cleared by opening, closing or saving). */
  const [suggestingId, setSuggestingId] = useState<string | null>(null);
  const [suggestion, setSuggestion] = useState<PendingSuggestion | null>(null);

  const listSeq = useRef(0);   // an answer for an older request (or another traveller) is dropped
  const openSeq = useRef(0);

  async function refresh() {
    const mine = ++listSeq.current;
    if (!traveller) {
      setRoutes([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const found = await latestApi.current.listRoutes(traveller);
      if (listSeq.current === mine) {
        setRoutes(found);
        setLoaded(true);
      }
    } catch (e) {
      if (listSeq.current === mine) setStatus(failure(message(e)));
    } finally {
      if (listSeq.current === mine) setLoading(false);
    }
  }

  // A different traveller (or a different login, via reloadKey) is a different list.
  useEffect(() => {
    openSeq.current++;
    setOpened(null);
    setOutOfDate(false);
    setRoutes([]);
    setLoaded(false);
    setStatus(info(""));
    void refresh();
  }, [traveller, reloadKey]);

  async function open(routeId: string): Promise<boolean> {
    const mine = ++openSeq.current;
    setSuggestion(null);
    try {
      const route = await latestApi.current.getRoute(routeId);
      if (openSeq.current !== mine) return false;
      setOpened({ ...route });   // always a new object: <RoutePlanner viewing> shows each new one, even the same route again
      setOutOfDate(false);
      setStatus(info(""));   // the opened route is on screen; no need to say so as well
      return true;
    } catch (e) {
      if (openSeq.current === mine) setStatus(failure(message(e)));
      return false;
    }
  }

  function close() {
    openSeq.current++;
    setOpened(null);
    setOutOfDate(false);
    setSuggestion(null);
  }

  /**
   * Opens the route and asks the AI for simpler wording. The answer waits in `suggestion` for the editor; nothing
   * is saved until the caregiver saves it there.
   */
  async function suggest(routeId: string) {
    setSuggestingId(routeId);
    try {
      if (!(await open(routeId))) return;
      setStatus(info("Simplifying the wording with AI. This can take up to a minute..."));
      const mine = openSeq.current;     // another route opened meanwhile: drop this answer
      const answer = await latestApi.current.suggestEasyRead(routeId);
      if (openSeq.current !== mine) return;
      setSuggestion({ ...answer, routeId });
      setStatus(answer.provider === "template"
        ? failure("The AI isn't available right now, so the editor shows the standard wording.")
        : info("Check the AI's wording for each step, change anything you like, then save."));
    } catch (e) {
      setStatus(failure(message(e)));
    } finally {
      setSuggestingId(null);
    }
  }

  /** Stores a photo for a step of the open route; null (with the reason in `status`) if it was refused. */
  async function uploadPhoto(photo: File): Promise<UploadedPhoto | null> {
    if (!opened) return null;
    try {
      return await latestApi.current.uploadStepPhoto(opened.routeId, photo);
    } catch (e) {
      setStatus(failure(message(e)));
      return null;
    }
  }

  /**
   * Saves changes to the open route. The service bumps the version and clears the approval, so the
   * route has to be approved again (and trips made on the old version stop working).
   */
  async function update(changes: RouteChanges): Promise<SavedRoute | null> {
    const base = opened;
    if (!base) return null;
    const body: RouteUpdate = {
      travellerId: base.travellerId,
      caregiverId: base.caregiverId,
      name: changes.name.trim() || base.name,
      origin: base.origin,
      destination: base.destination,
      steps: base.steps.map((step, i) => ({
        ...step,
        easyRead: changes.easyRead[i] === undefined ? step.easyRead : changes.easyRead[i],
        photoKey: changes.photoKey?.[i] === undefined ? step.photoKey : changes.photoKey[i],
        landmarks: changes.landmarks?.[i] === undefined ? step.landmarks : changes.landmarks[i],
      })),
      path: base.path,
      version: base.version,
    };
    setSaving(true);
    try {
      const route = await latestApi.current.updateRoute(base.routeId, body);
      setOpened({ ...route });
      setSuggestion(null);
      setOutOfDate(false);
      setRoutes((rs) => rs.map((r) => (r.routeId === route.routeId ? route : r)));
      setStatus(info(`Saved version ${route.version} of ${route.name}. It needs approving again.`));
      return route;
    } catch (e) {
      setOutOfDate(e instanceof ApiError && e.status === 409);
      setStatus(failure(message(e)));
      return null;
    } finally {
      setSaving(false);
    }
  }

  async function approve(routeId: string) {
    setApprovingId(routeId);
    try {
      const route = await latestApi.current.approveRoute(routeId);
      setRoutes((rs) => rs.map((r) => (r.routeId === route.routeId ? route : r)));
      setStatus(info(`${route.routeId} approved (version ${route.version}).`));
    } catch (e) {
      setStatus(failure(message(e)));
    } finally {
      setApprovingId(null);
    }
  }

  /** Deletes a route (as the version the list shows). True if it is gone. */
  async function remove(route: SavedRoute): Promise<boolean> {
    setDeletingId(route.routeId);
    try {
      await latestApi.current.deleteRoute(route.routeId, route.version);
      setRoutes((rs) => rs.filter((r) => r.routeId !== route.routeId));
      setOpened((o) => (o?.routeId === route.routeId ? null : o));
      setStatus(info(`Deleted ${route.name} (${route.routeId}).`));
      return true;
    } catch (e) {
      setStatus(failure(message(e)));
      // Changed or already deleted by someone else: the list on screen is out of date.
      if (e instanceof ApiError && (e.status === 409 || e.status === 404)) void refresh();
      return false;
    } finally {
      setDeletingId(null);
    }
  }

  return {
    traveller, routes, loaded, loading, status, opened, approvingId, deletingId, saving, outOfDate, suggestingId, suggestion,
    refresh, open, close, approve, update, remove, suggest, uploadPhoto,
  };
}

export type SavedRoutesState = ReturnType<typeof useSavedRoutes>;
