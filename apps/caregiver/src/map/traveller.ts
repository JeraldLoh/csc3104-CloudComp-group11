// The traveller on a map, drawn the same way on the dashboard (profiles/LiveMap.tsx) and the Trips tab
// (planner/MapView.tsx, given it by planner/TripMap.tsx): one place for how they look, so the two stay alike.
//
//   drawTraveller(layer, traveller)                 their trail, dot and label onto a Leaflet layer
//   locationAlert(incidents, tripId)                the open alert about where they are on that trip, as words
//   useLocationAlert(caregiverId, travellerId, tripId)   the same, loaded for a page without the live alerts
//
// While an alert about where they are is open ("May have gone off route"), the dot turns amber, grows a little and
// its label says so. Before the phone's first check-in (`guessed`: where their step starts) it is grey.
import * as L from "leaflet";
import { useEffect, useState } from "react";
import { fetchIncidents } from "../alerts/api";
import { problemTitle, type Incident } from "../alerts/incidents";
import "./traveller.css";

export interface Traveller {
  lat: number;
  lng: number;
  trail: [number, number][];
  label: string;
  /** Not from a check-in (where their step starts): drawn greyed out. */
  guessed?: boolean;
  /** An open alert about where they are ("May have gone off route"): drawn amber, and the label says it. */
  alert?: string | null;
}

export const TRAVELLER_COLORS = { here: "#2563eb", guessed: "#6b7280", alert: "#d97706", trail: "#0f172a" } as const;

/** Alerts about where the traveller is: the ones the map shows on their dot. */
export const LOCATION_ALERTS = new Set(["off_route", "wrong_direction", "missed_stop"]);

/** The label as shown: "Alex: May have gone off route" while an alert is open. */
export function travellerLabel(t: Pick<Traveller, "label" | "alert">): string {
  return t.alert ? `${t.label}: ${t.alert}` : t.label;
}

/** Draws the traveller's trail (dark, so it shows over any step's colour), then their dot and label. */
export function drawTraveller(layer: L.LayerGroup, t: Traveller): void {
  const fill = t.alert ? TRAVELLER_COLORS.alert : t.guessed ? TRAVELLER_COLORS.guessed : TRAVELLER_COLORS.here;
  if (t.trail.length > 1) L.polyline(t.trail, { color: TRAVELLER_COLORS.trail, weight: 4, opacity: 0.5 }).addTo(layer);
  const label = document.createElement("span");           // as text, never HTML: names come from people
  label.textContent = travellerLabel(t);
  L.circleMarker([t.lat, t.lng], { radius: t.alert ? 11 : 9, color: "#fff", weight: 3, fillColor: fill, fillOpacity: 1 })
    .bindTooltip(label, { permanent: true, direction: "top", offset: [0, -8], className: t.alert ? "map-traveller--alert" : "" })
    .addTo(layer);
}

/** The open alert about where they are on this trip, in words, or null. One without a trip id counts for any trip. */
export function locationAlert(incidents: Pick<Incident, "problemType" | "state" | "tripId">[], tripId: string | null | undefined): string | null {
  const found = incidents.find((i) => LOCATION_ALERTS.has(i.problemType) && i.state !== "resolved"
    && (!i.tripId || !tripId || i.tripId === tripId));
  return found ? problemTitle(found.problemType) : null;
}

const ALERT_POLL_MS = 10_000;

/** locationAlert() for a page without the dashboard's live alerts (the Trips tab): asks the Alert Service every few
 * seconds while a trip is shown. A failed check keeps the last answer: the alert and push still reach the caregiver. */
export function useLocationAlert(caregiverId: string, travellerId: string | null | undefined, tripId: string | null | undefined): string | null {
  const [alert, setAlert] = useState<string | null>(null);
  useEffect(() => {
    setAlert(null);
    if (!caregiverId || !travellerId || !tripId) return;
    let current = true;
    const look = () => fetchIncidents(travellerId, caregiverId)
      .then((list) => current && setAlert(locationAlert(list, tripId)))
      .catch(() => undefined);
    void look();
    const timer = setInterval(() => void look(), ALERT_POLL_MS);
    return () => {
      current = false;
      clearInterval(timer);
    };
  }, [caregiverId, travellerId, tripId]);
  return alert;
}
