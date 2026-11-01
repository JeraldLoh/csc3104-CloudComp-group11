// Where a traveller is now (issue 5.5): their position and recent trail on OneMap's map, as in the planner.
// OneMap's attribution must stay.
import * as L from "leaflet";
import "leaflet/dist/leaflet.css";
import { useEffect, useRef } from "react";
import type { LiveTrip } from "./api";

const ZOOM = 16;

export function LiveMap({ name, trip }: { name: string; trip: LiveTrip }) {
  const host = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const layer = useRef<L.LayerGroup | null>(null);

  useEffect(() => {
    if (!host.current) return;
    const created = L.map(host.current).setView([trip.lat, trip.lng], ZOOM);
    L.tileLayer("https://www.onemap.gov.sg/maps/tiles/Default/{z}/{x}/{y}.png", {
      minZoom: 11, maxZoom: 19,
      attribution: '<a href="https://www.onemap.gov.sg/">OneMap</a> &copy; contributors &middot; <a href="https://www.sla.gov.sg/">Singapore Land Authority</a>',
    }).addTo(created);
    map.current = created;
    layer.current = L.layerGroup().addTo(created);
    return () => {
      created.remove();
      map.current = null;
      layer.current = null;
    };
  }, []); // made once; the effect below follows the traveller

  useEffect(() => {
    if (!map.current || !layer.current) return;
    layer.current.clearLayers();
    L.polyline(trip.trail, { color: "#2563eb", weight: 4, opacity: 0.6 }).addTo(layer.current);
    L.circleMarker([trip.lat, trip.lng], { radius: 9, color: "#fff", weight: 3, fillColor: "#2563eb", fillOpacity: 1 })
      .bindTooltip(name, { permanent: true, direction: "top", offset: [0, -8] })
      .addTo(layer.current);
    map.current.panTo([trip.lat, trip.lng]); // not setView: the caregiver may have zoomed
  }, [name, trip.lat, trip.lng, trip.trail]);

  return <div ref={host} className="pf__map" role="img" aria-label={`Map showing where ${name} is`} />;
}
