import { useState } from "react";
import type { Landmark } from "../journey/types";
import { landmarkSeenIn } from "./format";

interface Props {
  url: string | null | undefined;
  alt: string;
  /** Shown instead of the photo when there is none or its link doesn't load (expired, or the file is gone). */
  fallback: React.ReactNode;
  /** Shown under the photo when it does load (e.g. "only an old photo was found: check it"). */
  note?: React.ReactNode;
}

/** A step's photo, with an optional note under it, or `fallback`. */
export function StepPhoto({ url, alt, fallback, note }: Props) {
  const [failed, setFailed] = useState<string | null>(null);         // the url that failed, so a new one is tried
  if (!url || failed === url) return <>{fallback}</>;
  return (
    <>
      <img className="rp__photo" src={url} alt={alt} loading="lazy" onError={() => setFailed(url)} />
      {note}
    </>
  );
}

/**
 * The landmarks a step's wording names, each with its photo, so the caregiver sees what the traveller looks for.
 * One whose photo would repeat another (`sameView`) is only named, with where it can be seen.
 */
export function LandmarkPhotos({ landmarks }: { landmarks: Landmark[] }) {
  if (landmarks.length === 0) return null;
  const own = landmarks.filter((m) => !m.sameView);
  const elsewhere = landmarks.filter((m) => m.sameView);
  return (
    <>
      {own.length > 0 && (
        <div className="rp__landmarks">
          {own.map((m) => (
            <figure key={m.name} className="rp__landmark">
              <StepPhoto url={m.photoUrl} alt={m.name} fallback={<div className="rp__meta">No street photo of it.</div>} />
              <figcaption className="rp__meta">{`Look for ${m.name}`}</figcaption>
            </figure>
          ))}
        </div>
      )}
      {elsewhere.map((m) => <div key={m.name} className="rp__meta">{landmarkSeenIn(m)}</div>)}
    </>
  );
}
