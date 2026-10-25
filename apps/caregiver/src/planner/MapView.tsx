// The map: OneMap tiles, the two pins, and the selected option drawn step by step.
// Wraps Leaflet directly (no react-leaflet), so the map is created once and the props only
// change what is drawn on it.
import { useEffect, useRef } from "react";
import * as L from "leaflet";
import "leaflet/dist/leaflet.css";
import iconUrl from "leaflet/dist/images/marker-icon.png";
import iconRetinaUrl from "leaflet/dist/images/marker-icon-2x.png";
import shadowUrl from "leaflet/dist/images/marker-shadow.png";
import type { PlanOption, SavedRoute, StepMode } from "../journey/types";
import type { Focus, Point } from "./usePlanner";

// Leaflet works out its marker image paths from the CSS file name, which a bundler breaks.
delete (L.Icon.Default.prototype as unknown as { _getIconUrl?: unknown })._getIconUrl;
L.Icon.Default.mergeOptions({ iconUrl, iconRetinaUrl, shadowUrl });

export const MODE_COLORS: Record<StepMode, string> = { walk: "#6b7280", bus: "#1d6fd6", train: "#d6331d" };
const SINGAPORE: L.LatLngTuple = [1.3521, 103.8198];

/** Leaflet reads a string tooltip/popup as HTML. Place names and LTA notices are not trusted, so pass text nodes. */
function text(content: string): HTMLElement {
  const node = document.createElement("div");
  node.textContent = content;
  return node;
}

export interface MapViewProps {
  from: Point | null;
  to: Point | null;
  /** A planned option to draw, step by step... */
  option: PlanOption | null;
  /** ...or a saved route (shown instead of the option if both are given). */
  saved?: SavedRoute | null;
  focus: Focus | null;
  onMapClick: (lat: number, lng: number) => void;
}

export function MapView({ from, to, option, saved = null, focus, onMapClick }: MapViewProps) {
  const host = useRef<HTMLDivElement>(null);
  const layers = useRef<{ map: L.Map; pins: L.LayerGroup; route: L.LayerGroup } | null>(null);
  const clicked = useRef(onMapClick);
  useEffect(() => { clicked.current = onMapClick; });

  // Create the map once.
  useEffect(() => {
    if (!host.current) return;
    const map = L.map(host.current).setView(SINGAPORE, 12);
    L.tileLayer("https://www.onemap.gov.sg/maps/tiles/Default/{z}/{x}/{y}.png", {
      minZoom: 11, maxZoom: 19,
      attribution: '<a href="https://www.onemap.gov.sg/">OneMap</a> &copy; contributors &middot; <a href="https://www.sla.gov.sg/">Singapore Land Authority</a>',
    }).addTo(map);
    layers.current = { map, pins: L.layerGroup().addTo(map), route: L.layerGroup().addTo(map) };
    map.on("click", (e: L.LeafletMouseEvent) => clicked.current(e.latlng.lat, e.latlng.lng));
    // The planner can sit in a layout that changes size (a collapsing sidebar, a tab).
    const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => map.invalidateSize());
    resize?.observe(host.current);
    return () => {
      resize?.disconnect();
      map.remove();
      layers.current = null;
    };
  }, []);

  useEffect(() => {
    const l = layers.current;
    if (!l) return;
    l.pins.clearLayers();
    for (const [p, name] of [[from, "From"], [to, "To"]] as const) {
      if (p) L.marker([p.lat, p.lng]).bindTooltip(name, { permanent: true, direction: "top" }).addTo(l.pins);
    }
  }, [from, to]);

  useEffect(() => {
    const l = layers.current;
    if (!l) return;
    l.route.clearLayers();
    const bounds = L.latLngBounds([]);
    if (saved) {
      // A saved route keeps one joined path, not one per step.
      for (const point of saved.path) bounds.extend(point);
      L.polyline(saved.path, { color: MODE_COLORS.bus, weight: 5, opacity: 0.85 }).addTo(l.route);
      saved.steps.forEach((step, i) => {
        const at = step.from.lat != null && step.from.lng != null ? ([step.from.lat, step.from.lng] as L.LatLngTuple) : saved.path[0];
        if (!at) return;
        bounds.extend(at);
        L.circleMarker(at, { radius: 6, color: MODE_COLORS[step.mode], fillColor: "#fff", fillOpacity: 1, weight: 3 })
          .bindPopup(text(`${i + 1}. ${step.instruction}`)).addTo(l.route);
      });
      if (bounds.isValid()) l.map.fitBounds(bounds, { padding: [40, 40] });
      return;
    }
    if (!option) return;
    option.steps.forEach((step, i) => {
      const path = option.stepPaths[i];
      if (!path?.length) return;
      for (const point of path) bounds.extend(point);
      const color = MODE_COLORS[step.mode];
      L.polyline(path, { color, weight: step.mode === "walk" ? 4 : 6, opacity: 0.9, dashArray: step.mode === "walk" ? "2 8" : undefined })
        .bindTooltip(text(step.instruction)).addTo(l.route);
      L.circleMarker(path[0], { radius: 6, color, fillColor: "#fff", fillOpacity: 1, weight: 3 })
        .bindPopup(text(`${i + 1}. ${step.instruction}`)).addTo(l.route);
    });
    for (const w of option.warnings) {
      const at = w.stepIndex != null ? option.stepPaths[w.stepIndex] : null;
      if (at?.length) {
        L.circleMarker(at[Math.floor(at.length / 2)], { radius: 9, color: "#b45309", fillColor: "#f59e0b", fillOpacity: 0.7 })
          .bindPopup(text(w.message)).addTo(l.route);
      }
    }
    if (bounds.isValid()) l.map.fitBounds(bounds, { padding: [40, 40] });
  }, [option, saved]);

  useEffect(() => {
    if (focus) layers.current?.map.setView([focus.lat, focus.lng], focus.zoom);
  }, [focus]);

  return <div ref={host} className="rp__map" role="application" aria-label="Map" />;
}
