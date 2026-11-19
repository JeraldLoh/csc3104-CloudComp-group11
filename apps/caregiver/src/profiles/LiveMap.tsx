// Where a traveller is now (issue 5.5): their position and recent trail on OneMap's map, as in the planner.
// With no `trip` (under way, but no check-in yet) it shows just the planned route, zoomed to fit it.
// OneMap's attribution must stay.
import * as L from "leaflet";
import "leaflet/dist/leaflet.css";
import { useEffect, useRef } from "react";
import { MODE_COLORS } from "../planner/MapView";
import { drawTraveller } from "../map/traveller";
import type { LiveTrip } from "./api";
import type { Leg } from "./useTripPath";

const ZOOM = 16;
const SINGAPORE: [number, number] = [1.3521, 103.8198];   // until the route arrives

export function LiveMap({ name, trip, path, legs = [], alert = null }: {
  name: string; trip: LiveTrip | null; path: [number, number][] | null; legs?: Leg[];
  /** An open alert about where they are ("May have gone off route"): the dot turns amber and says it. */
  alert?: string | null;
}) {
  const host = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const layer = useRef<L.LayerGroup | null>(null);
  const route = useRef<L.LayerGroup | null>(null);

  useEffect(() => {
    if (!host.current) return;
    const created = trip ? L.map(host.current).setView([trip.lat, trip.lng], ZOOM) : L.map(host.current).setView(SINGAPORE, 11);
    L.tileLayer("https://www.onemap.gov.sg/maps/tiles/Default/{z}/{x}/{y}.png", {
      minZoom: 11, maxZoom: 19,
      attribution: '<a href="https://www.onemap.gov.sg/">OneMap</a> &copy; contributors &middot; <a href="https://www.sla.gov.sg/">Singapore Land Authority</a>',
    }).addTo(created);
    map.current = created;
    // The box's size follows the window (profiles.css): redraw the tiles to fill it, or part of it stays grey
    const resized = typeof ResizeObserver !== "undefined" ? new ResizeObserver(() => created.invalidateSize()) : null;
    resized?.observe(host.current);
    // added first so the planned route sits under the trail
    route.current = L.layerGroup().addTo(created);
    layer.current = L.layerGroup().addTo(created);
    return () => {
      resized?.disconnect();
      created.remove();
      map.current = null;
      route.current = null;
      layer.current = null;
    };
  }, []); // made once; the effect below follows the traveller

  useEffect(() => {
    if (!map.current || !layer.current) return;
    layer.current.clearLayers();
    if (!trip) return;
    // as on the Trips tab's map (map/traveller.ts)
    drawTraveller(layer.current, { lat: trip.lat, lng: trip.lng, trail: trip.trail, label: name, alert });
    map.current.panTo([trip.lat, trip.lng]); // not setView: the caregiver may have zoomed
  }, [name, trip?.lat, trip?.lng, trip?.trail, alert]);

  // The planned route under the trail: each step in its own colour, as on the planner's map (walks dotted), with
  // where it starts and ends. Only the saved route's line known: one blue line.
  const following = trip !== null;
  useEffect(() => {
    if (!route.current) return;
    route.current.clearLayers();
    if (!path || path.length === 0) return;
    const drawn = legs.length ? legs : [{ mode: "bus" as const, label: "", path }];
    for (const leg of drawn) {
      const walk = leg.mode === "walk";
      const line = L.polyline(leg.path, { color: MODE_COLORS[leg.mode], weight: walk ? 4 : 6, opacity: 0.9,
        dashArray: walk ? "2 8" : undefined }).addTo(route.current);
      if (leg.label) line.bindTooltip(leg.label);
    }
    L.circleMarker(path[0], { radius: 6, color: "#15803d", weight: 3, fillColor: "#fff", fillOpacity: 1 })
      .bindTooltip("Start").addTo(route.current);
    L.circleMarker(path[path.length - 1], { radius: 7, color: "#fff", weight: 3, fillColor: "#15803d", fillOpacity: 1 })
      .bindTooltip("Destination").addTo(route.current);
    if (!following) map.current?.fitBounds(L.latLngBounds(path), { padding: [24, 24] });   // nobody to follow yet
  }, [path, legs, following]);

  const label = !trip ? `Map of ${name}'s planned route`
    : `Map showing where ${name} is${alert ? `. ${alert}` : ""}`;
  return <div ref={host} className="pf__map" role="img" aria-label={label} />;
}
