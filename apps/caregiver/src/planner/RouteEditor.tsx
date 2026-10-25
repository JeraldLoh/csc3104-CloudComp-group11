import { useId, useState } from "react";
import type { SavedRoute } from "../journey/types";
import type { RouteChanges } from "./useSavedRoutes";

interface Props {
  route: SavedRoute;
  /** Planned or active trips made on this route. Saving stops them working. */
  affectedTrips: number;
  saving: boolean;
  onSave: (changes: RouteChanges) => void;
  onCancel: () => void;
}

/**
 * Change a saved route's name and the easy-read wording the traveller sees for each step.
 * The steps themselves (where to walk, which bus) come from planning, so they are shown, not edited.
 */
export function RouteEditor({ route, affectedTrips, saving, onSave, onCancel }: Props) {
  const nameId = useId();
  const [name, setName] = useState(route.name);
  const [easyRead, setEasyRead] = useState(route.steps.map((s) => s.easyRead ?? ""));

  const changed = name.trim() !== route.name || easyRead.some((text, i) => text.trim() !== (route.steps[i].easyRead ?? ""));
  const canSave = changed && name.trim().length > 0 && !saving;

  return (
    <form
      className="rp__editor"
      onSubmit={(e) => {
        e.preventDefault();
        if (!canSave) return;
        // Emptied = null: the service writes its own wording for that step.
        onSave({ name, easyRead: easyRead.map((text) => text.trim() || null) });
      }}
    >
      <div className="rp__warning">
        Saving makes a new version and clears the approval: approve it again, then make a new trip.
        {affectedTrips > 0 && ` ${affectedTrips} planned or active trip${affectedTrips > 1 ? "s" : ""} on this route will stop working.`}
      </div>

      <label htmlFor={nameId}>Route name</label>
      <input id={nameId} value={name} maxLength={100} onChange={(e) => setName(e.target.value)} />

      <div className="rp__meta">The wording the traveller reads. Leave a step empty to use the automatic wording.</div>
      {route.steps.map((step, i) => (
        <div key={step.stepIndex} className={`rp__step rp__step--${step.mode}`}>
          <div className="rp__meta">{`${i + 1}. ${step.instruction}`}</div>
          <textarea
            aria-label={`Easy-read wording for step ${i + 1}`}
            rows={2}
            maxLength={300}
            value={easyRead[i]}
            onChange={(e) => setEasyRead((list) => list.map((t, j) => (j === i ? e.target.value : t)))}
          />
        </div>
      ))}

      <div className="rp__row">
        <button type="submit" className="rp__btn" disabled={!canSave}>{saving ? "Saving..." : "Save changes"}</button>
        <button type="button" className="rp__btn rp__btn--secondary" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}
