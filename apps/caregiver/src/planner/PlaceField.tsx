// A search box for a place, with the pinned result in the box and other results in a list.
import { useId } from "react";
import type { PlaceResult } from "../journey/types";
import type { Point } from "./usePlanner";

interface Props {
  label: string;
  value: string;
  point: Point | null;
  candidates: PlaceResult[];
  onChange: (text: string) => void;
  onSearch: () => void;
  onPick: (place: PlaceResult) => void;
}

export function PlaceField({ label, value, point, candidates, onChange, onSearch, onPick }: Props) {
  const id = useId();
  // The pinned result is already in the box, so the list only offers the others. It is only
  // relevant while the pin is one of the results (not after a map click or an edited box).
  const pinned = point ? candidates.some((c) => c.lat === point.lat && c.lng === point.lng) : false;
  const others = pinned && point ? candidates.filter((c) => c.lat !== point.lat || c.lng !== point.lng) : [];
  return (
    <div className="rp__field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        value={value}
        placeholder="Place, address or postal code, then Enter"
        autoComplete="off"
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") onSearch(); }}
      />
      {others.length > 0 && (
        <select
          aria-label={`${label}: other matches`}
          value=""
          onChange={(e) => onPick(others[Number(e.target.value)])}
        >
          <option value="" disabled>{`Not this one? ${others.length} other match${others.length > 1 ? "es" : ""}`}</option>
          {others.map((p, i) => (
            <option key={i} value={i}>{p.name + (p.postalCode ? ` (${p.postalCode})` : "")}</option>
          ))}
        </select>
      )}
    </div>
  );
}
