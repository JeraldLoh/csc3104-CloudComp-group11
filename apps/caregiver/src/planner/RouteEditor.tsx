import { type KeyboardEvent, useId, useLayoutEffect, useRef, useState } from "react";
import type { EasyReadSuggestion, SavedRoute, UploadedPhoto } from "../journey/types";
import { BULLET, fromBulletLines, isRoughPhoto, photoHint, roughPhotoHint, toBulletLines } from "./format";
import { StepPhoto } from "./StepPhoto";
import type { RouteChanges } from "./useSavedRoutes";

interface Props {
  route: SavedRoute;
  /** Planned or active trips made on this route. Saving stops them working. */
  affectedTrips: number;
  saving: boolean;
  /** AI wording to start from instead of the saved wording ("Simplify with AI"). */
  suggested?: EasyReadSuggestion | null;
  /** Stores a photo and gives back its key, or null if it was refused (the reason is shown by the caller). */
  onUploadPhoto: (photo: File) => Promise<UploadedPhoto | null>;
  onSave: (changes: RouteChanges) => void;
  onCancel: () => void;
}

/** Longest wording a step may have once saved (as sentences), as before. */
const MAX_EASY_READ = 300;

/** A step's photo in the editor: `key` is what will be saved, `url` what is shown (null: none yet). */
interface Photo {
  key: string | null;
  url: string | null;
}

/**
 * Change a saved route's name, and the easy-read wording and photo the traveller sees for each step.
 * The steps themselves (where to walk, which bus) come from planning, so they are shown, not edited.
 */
export function RouteEditor({ route, affectedTrips, saving, suggested, onUploadPhoto, onSave, onCancel }: Props) {
  const nameId = useId();
  const [name, setName] = useState(route.name);
  const aiFor = (stepIndex: number) => suggested?.steps.find((s) => s.stepIndex === stepIndex);
  // Each step's wording as the box shows it: one "• point" per line (stored as one sentence per point).
  const [easyRead, setEasyRead] = useState(route.steps.map((s) => toBulletLines(aiFor(s.stepIndex)?.easyRead ?? s.easyRead)));
  const [photos, setPhotos] = useState<Photo[]>(route.steps.map((s) => ({ key: s.photoKey ?? null, url: s.photoUrl ?? null })));
  const [uploading, setUploading] = useState<number | null>(null);

  const setStepText = (i: number, text: string) => setEasyRead((list) => list.map((t, j) => (j === i ? text : t)));
  const photoChanged = (i: number) => photos[i].key !== (route.steps[i].photoKey ?? null);
  // Compared as sentences, so opening a route saved as one paragraph doesn't count as a change.
  const wordingChanged = (i: number) => fromBulletLines(easyRead[i]) !== fromBulletLines(toBulletLines(route.steps[i].easyRead));
  const tooLong = easyRead.map((lines) => fromBulletLines(lines).length > MAX_EASY_READ);
  const changed = name.trim() !== route.name
    || route.steps.some((_, i) => wordingChanged(i) || photoChanged(i));
  const canSave = changed && name.trim().length > 0 && !saving && uploading === null && !tooLong.some(Boolean);

  // Where the caret goes once a new bullet is in the box (set right after React updates it, before the next key).
  const caretAfter = useRef<{ box: HTMLTextAreaElement; at: number } | null>(null);
  useLayoutEffect(() => {
    if (!caretAfter.current) return;
    const { box, at } = caretAfter.current;
    caretAfter.current = null;
    box.setSelectionRange(at, at);
  });

  /** Enter starts a new bullet, as in a list. */
  function newBullet(i: number, e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key !== "Enter" || e.shiftKey) return;
    e.preventDefault();
    const { selectionStart: start, selectionEnd: end, value } = e.currentTarget;
    caretAfter.current = { box: e.currentTarget, at: start + 1 + BULLET.length };
    setStepText(i, `${value.slice(0, start)}\n${BULLET}${value.slice(end)}`);
  }

  async function replacePhoto(i: number, file: File | undefined) {
    if (!file) return;
    setUploading(i);
    try {
      const stored = await onUploadPhoto(file);
      if (stored) setPhotos((list) => list.map((p, j) => (j === i ? { key: stored.photoKey, url: stored.photoUrl } : p)));
    } finally {
      setUploading(null);
    }
  }

  return (
    <form
      className="rp__editor"
      onSubmit={(e) => {
        e.preventDefault();
        if (!canSave) return;
        onSave({
          name,
          // Unchanged = undefined (kept as it is); emptied = null: the service writes its own wording for that step.
          easyRead: easyRead.map((lines, i) => (wordingChanged(i) ? fromBulletLines(lines) || null : undefined)),
          // Unchanged = undefined (kept as it is); null = the service adds a Street View photo again.
          photoKey: photos.map((p, i) => (photoChanged(i) ? p.key : undefined)),
        });
      }}
    >
      <div className="rp__warning">
        Saving makes a new version and clears the approval: approve it again, then make a new trip.
        {affectedTrips > 0 && ` ${affectedTrips} planned or active trip${affectedTrips > 1 ? "s" : ""} on this route will stop working.`}
      </div>
      {suggested && (
        <div className={suggested.provider === "template" ? "rp__warning" : "rp__meta"}>
          {suggested.provider === "template"
            ? "The AI wasn't available, so each step shows the standard wording."
            : "Suggested by AI. Check each step before saving: the traveller will read exactly this."}
        </div>
      )}

      <label htmlFor={nameId}>Route name</label>
      <input id={nameId} value={name} maxLength={100} onChange={(e) => setName(e.target.value)} />

      <div className="rp__meta">
        The wording the traveller reads, one action per bullet point (Enter starts a new one). Leave a step empty to
        use the automatic wording.
      </div>
      {route.steps.map((step, i) => {
        const photo = photos[i];
        const uploaded = photo.key?.startsWith("media/uploads/") ?? false;
        return (
          <div key={step.stepIndex} className={`rp__step rp__step--${step.mode}`}>
            <div className="rp__meta">{`${i + 1}. ${step.instruction}`}</div>
            <textarea
              className="rp__points-edit"
              aria-label={`Easy-read wording for step ${i + 1}, one point per line`}
              rows={Math.max(2, easyRead[i].split("\n").length)}
              value={easyRead[i]}
              onFocus={() => { if (!easyRead[i].trim()) setStepText(i, BULLET); }}
              onKeyDown={(e) => newBullet(i, e)}
              onChange={(e) => setStepText(i, e.target.value)}
            />
            {tooLong[i] && <div className="rp__warning">{`Too long: keep this step under ${MAX_EASY_READ} characters.`}</div>}
            {suggested && suggested.provider !== "template" && aiFor(step.stepIndex)?.fallback && (
              <div className="rp__meta">The AI's version didn't keep every stop and number, so this is the standard wording.</div>
            )}

            <div className="rp__photo-edit">
              <StepPhoto
                url={photo.url}
                alt={`Where step ${i + 1} ends`}
                fallback={photoChanged(i) && step.mode !== "train"     // "Use Street View photo" was just pressed
                  ? <div className="rp__meta">No photo. A Street View photo is added on save when there is one.</div>
                  : <div className="rp__photo-hint">{photoHint(step)}</div>}
                note={isRoughPhoto(photo.key) && <div className="rp__photo-hint">{roughPhotoHint(step)}</div>}
              />
              <div className="rp__row">
                <label className="rp__btn rp__btn--secondary rp__btn--small rp__file">
                  {uploading === i ? "Uploading..." : "Replace photo"}
                  <input
                    type="file"
                    accept="image/jpeg,image/png,image/webp"
                    disabled={uploading !== null}
                    aria-label={`Replace the photo for step ${i + 1}`}
                    onChange={(e) => { void replacePhoto(i, e.target.files?.[0]); e.target.value = ""; }}
                  />
                </label>
                {uploaded && (
                  <button
                    type="button"
                    className="rp__btn rp__btn--secondary rp__btn--small"
                    onClick={() => setPhotos((list) => list.map((p, j) => (j === i ? { key: null, url: null } : p)))}
                  >
                    {step.mode === "train" ? "Remove my photo" : "Use Street View photo"}
                  </button>
                )}
              </div>
            </div>
          </div>
        );
      })}

      <div className="rp__row">
        <button type="submit" className="rp__btn" disabled={!canSave}>{saving ? "Saving..." : "Save changes"}</button>
        <button type="button" className="rp__btn rp__btn--secondary" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}
