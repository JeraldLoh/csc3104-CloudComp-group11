// A map view of a planned route, using Leaflet and OneMap tiles
import { useEffect, useRef } from "react";
import * as L from "leaflet";
import "leaflet/dist/leaflet.css";
import iconUrl from "leaflet/dist/images/marker-icon.png";
import iconRetinaUrl from "leaflet/dist/images/marker-icon-2x.png";
import shadowUrl from "leaflet/dist/images/marker-shadow.png";
import type { PlanOption, SavedRoute, StepMode } from "../journey/types";
import type { Focus, Point } from "./usePlanner";

// Leaflet's default icon URLs are wrong in a Webpack build, so fix them. See https://github.com/Leaflet/Leaflet/issues/4835
delete (L.Icon.Default.prototype as unknown as { _getIconUrl?: unknown })._getIconUrl;
L.Icon.Default.mergeOptions({ iconUrl, iconRetinaUrl, shadowUrl });

// The colors used for the different modes of travel, and the default map view (Singapore).
export const MODE_COLORS: Record<StepMode, string> = { walk: "#6b7280", bus: "#1d6fd6", train: "#d6331d" };
const SINGAPORE: L.LatLngTuple = [1.3521, 103.8198];
/** Where trains aren't running: amber, unlike any mode's colour. */
export const DISRUPTED = "#f59e0b";

// Create a div with text content, for Leaflet's popups and tooltips. 
function text(content: string): HTMLElement {
  const node = document.createElement("div");
  node.textContent = content;
  return node;
}

export interface MapViewProps {
  from: Point | null;
  to: Point | null;
  /** A plan option, or any steps with their lines (a trip's current plan). */
  option: Pick<PlanOption, "steps" | "stepPaths" | "warnings"> | null;
  saved?: SavedRoute | null;
  focus: Focus | null;
  /** Without it, clicking the map does nothing (e.g. while it follows a trip). */
  onMapClick?: (lat: number, lng: number) => void;
  /** The two pins' labels (default "From" and "To"). */
  pinLabels?: [string, string];
  /** A traveller on their way: a dot where they are now and a line through their last positions. */
  traveller?: Traveller | null;
  /** Room kept at the top when fitting a route, for something laid over the map there (default 40 px). */
  insetTop?: number;
  /** Parts of the route where trains aren't running, drawn over it in the disruption colour. */
  affected?: Affected[];
}

export interface Affected {
  path: [number, number][];
  message: string;
}
const NONE: Affected[] = [];

export interface Traveller {
  lat: number;
  lng: number;
  trail: [number, number][];
  label: string;
  /** Not from a check-in (where their step starts): drawn greyed out. */
  guessed?: boolean;
}

// A map view of a planned route, using Leaflet and OneMap tiles. 
// The route is either a saved one (with one joined path) or a plan option (with one path per step). 
// The map shows the "from" and "to" points, the route, and any warnings on it. 
// The caregiver can click on the map to set a point.
export function MapView({ from, to, option, saved = null, focus, onMapClick, pinLabels = ["From", "To"], traveller = null, insetTop = 40, affected = NONE }: MapViewProps) {
  const host = useRef<HTMLDivElement>(null);
  const layers = useRef<{ map: L.Map; pins: L.LayerGroup; route: L.LayerGroup; affected: L.LayerGroup; traveller: L.LayerGroup } | null>(null);
  const clicked = useRef(onMapClick);
  useEffect(() => { clicked.current = onMapClick; });

  useEffect(() => {
    if (!host.current) return;
    const map = L.map(host.current).setView(SINGAPORE, 12);
    L.tileLayer("https://www.onemap.gov.sg/maps/tiles/Default/{z}/{x}/{y}.png", {
      minZoom: 11, maxZoom: 19,
      attribution: '<a href="https://www.onemap.gov.sg/">OneMap</a> &copy; contributors &middot; <a href="https://www.sla.gov.sg/">Singapore Land Authority</a>',
    }).addTo(map);
    layers.current = { map, pins: L.layerGroup().addTo(map), route: L.layerGroup().addTo(map), affected: L.layerGroup().addTo(map),
      traveller: L.layerGroup().addTo(map) };
    map.on("click", (e: L.LeafletMouseEvent) => clicked.current?.(e.latlng.lat, e.latlng.lng));
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
    for (const [p, name] of [[from, pinLabels[0]], [to, pinLabels[1]]] as const) {
      if (p) L.marker([p.lat, p.lng]).bindTooltip(name, { permanent: true, direction: "top" }).addTo(l.pins);
    }
  }, [from, to, pinLabels[0], pinLabels[1]]);

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
      if (bounds.isValid()) l.map.fitBounds(bounds, { paddingTopLeft: [40, insetTop], paddingBottomRight: [40, 40] });
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
    if (bounds.isValid()) l.map.fitBounds(bounds, { paddingTopLeft: [40, insetTop], paddingBottomRight: [40, 40] });
  }, [option, saved]);

  // Over the route, without refitting (a breakdown starts and ends while the caregiver looks).
  useEffect(() => {
    const l = layers.current;
    if (!l) return;
    l.affected.clearLayers();
    for (const a of affected) {
      if (a.path.length < 2) continue;
      L.polyline(a.path, { color: DISRUPTED, weight: 11, opacity: 0.9 }).bindTooltip(text(a.message)).addTo(l.affected);
      L.polyline(a.path, { color: "#fff", weight: 3, opacity: 0.9, dashArray: "6 8" }).addTo(l.affected);
    }
  }, [affected]);

  // Its own layer, and no refitting: it moves every few seconds, and the caregiver may have zoomed in.
  useEffect(() => {
    const l = layers.current;
    if (!l) return;
    l.traveller.clearLayers();
    if (!traveller) return;
    const color = traveller.guessed ? MODE_COLORS.walk : "#2563eb";
    if (traveller.trail.length > 1) L.polyline(traveller.trail, { color, weight: 4, opacity: 0.6 }).addTo(l.traveller);
    L.circleMarker([traveller.lat, traveller.lng], { radius: 9, color: "#fff", weight: 3, fillColor: color, fillOpacity: 1 })
      .bindTooltip(text(traveller.label), { permanent: true, direction: "top", offset: [0, -8] }).addTo(l.traveller);
  }, [traveller?.lat, traveller?.lng, traveller?.trail, traveller?.label, traveller?.guessed]);

  // Only a focus asked for while this map is shown: one from before (the map is made again when its tab is
  // opened again) would undo fitting the route.
  const seenFocus = useRef(focus?.n);
  useEffect(() => {
    if (!focus || focus.n === seenFocus.current) return;
    seenFocus.current = focus.n;
    layers.current?.map.setView([focus.lat, focus.lng], focus.zoom);
  }, [focus]);

  return <div ref={host} className="rp__map" role="application" aria-label="Map" />;
}
