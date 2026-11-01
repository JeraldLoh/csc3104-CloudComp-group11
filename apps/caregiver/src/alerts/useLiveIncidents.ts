// Live incidents for the logged-in caregiver's travellers, for the dashboard. Existing ones load from
// GET /api/incidents; new ones and every change arrive instantly over GET /api/stream
// (Server-Sent Events). The Alert Service only lets a caregiver see their own travellers.
import { useCallback, useEffect, useRef, useState } from "react";
import { isCaregiverId, type MyTravellers } from "../profiles/useMyTravellers";
import { fetchIncidents, streamUrl } from "./api";
import { applyEvent, mergeIncidents, type AlertEvent, type ArrivalEvent, type Incident, type IncidentMap } from "./incidents";

const FRESH_MS = 4000; // how long a new alert stays highlighted
// The Alert Service pings every 15 s. Hearing nothing for this long means the connection is dead even
// if the browser thinks it's open (a proxy can hold it open after the service has gone), so reconnect.
const SILENCE_MS = 40_000;

export type Connection = "connecting" | "live" | "reconnecting";

// Mount with key={caregiverId}: switching caregiver starts from nothing and a new connection.
export function useLiveIncidents(caregiverId: string, my: MyTravellers) {
  const [incidents, setIncidents] = useState<IncidentMap>({});
  const [connection, setConnection] = useState<Connection>("connecting");
  const [error, setError] = useState<string | null>(null);
  const [loadedFor, setLoadedFor] = useState<string | null>(null); // which travellers the last good load covered
  const [fresh, setFresh] = useState<ReadonlySet<string>>(new Set());
  const [arrivals, setArrivals] = useState<ArrivalEvent[]>([]); // announced while the page is open, until dismissed
  const loggedIn = isCaregiverId(caregiverId);

  const travellerIds = my.travellers.map((t) => t.travellerId);
  const idsKey = travellerIds.join(",");
  const idsRef = useRef(travellerIds);
  idsRef.current = travellerIds;

  const reload = useCallback(async () => {
    const ids = idsRef.current;
    const stillCurrent = () => ids.join(",") === idsRef.current.join(",");
    try {
      const lists = await Promise.all(ids.map((id) => fetchIncidents(id, caregiverId)));
      setIncidents((current) => mergeIncidents(current, lists.flat()));
      setError(null);
      if (stillCurrent()) setLoadedFor(ids.join(","));
    } catch (e) {
      setError(`Couldn't load alerts: ${(e as Error).message}`);
      if (stillCurrent()) setLoadedFor(null);
    }
  }, [caregiverId]);

  const highlight = useCallback((incidentId: string) => {
    setFresh((s) => new Set(s).add(incidentId));
    setTimeout(() => setFresh((s) => {
      const next = new Set(s);
      next.delete(incidentId);
      return next;
    }), FRESH_MS);
  }, []);

  // Load alerts whenever the list of travellers changes (e.g. one was just added).
  useEffect(() => {
    if (loggedIn) void reload();
  }, [idsKey, loggedIn, reload]);

  // Live alerts. EventSource reconnects by itself when the connection drops; the silence check below
  // covers a connection that looks open but is dead. After every reconnect the list is reloaded,
  // so anything that happened while disconnected still shows up.
  useEffect(() => {
    if (!loggedIn) return;
    let source: EventSource;
    let openedBefore = false;
    let lastHeard = Date.now();

    const connect = () => {
      source = new EventSource(streamUrl(caregiverId));
      source.onopen = () => {
        lastHeard = Date.now();
        setConnection("live");
        if (openedBefore) void reload();
        openedBefore = true;
      };
      source.onerror = () => setConnection("reconnecting");
      source.addEventListener("ping", () => {
        lastHeard = Date.now();
      });
      source.onmessage = (msg) => {
        lastHeard = Date.now();
        const event: AlertEvent | ArrivalEvent = JSON.parse(msg.data);
        const about = "incident" in event ? event.incident.travellerId : event.travellerId;
        // The service already sends only this caregiver's travellers; this also covers AUTH_MODE=off.
        if (!idsRef.current.includes(about)) return;
        if (!("incident" in event)) {
          setArrivals((current) => [...current.filter((a) => a.tripId !== event.tripId), event]);
          return;
        }
        setIncidents((current) => applyEvent(current, event));
        if (event.event === "incident.opened" || event.event === "incident.reopened") {
          highlight(event.incident.incidentId);
        }
      };
    };

    connect();
    const silenceCheck = setInterval(() => {
      if (Date.now() - lastHeard < SILENCE_MS) return;
      source.close();
      setConnection("reconnecting");
      lastHeard = Date.now();
      connect();
    }, 5000);

    return () => {
      clearInterval(silenceCheck);
      source.close();
    };
  }, [caregiverId, loggedIn, reload, highlight]);

  const merge = useCallback((updated: Incident) => {
    setIncidents((current) => mergeIncidents(current, [updated]));
  }, []);

  const dismissArrival = useCallback((tripId: string) => {
    setArrivals((current) => current.filter((a) => a.tripId !== tripId));
  }, []);

  /** Only this caregiver's current travellers. */
  const mine = Object.values(incidents).filter((i) => travellerIds.includes(i.travellerId));
  /** False until the current travellers' alerts have loaded, and again if a reload fails: "no alerts" means nothing then. */
  const known = my.loaded && loadedFor === idsKey;

  return { mine, known, connection, error, setError, merge, fresh, loggedIn, arrivals, dismissArrival };
}
