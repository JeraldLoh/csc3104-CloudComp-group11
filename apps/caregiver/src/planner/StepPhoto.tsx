import { useState } from "react";
import type { Landmark } from "../journey/types";
import { landmarkSeenIn } from "./format";

interface Props {
  url: string | null | undefined;
  alt: string;
  fallback: React.ReactNode;
  note?: React.ReactNode;
}

// A photo the traveller sees for a step, or a placeholder if there is none or it fails to load.
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

// A traveller's landmarks: show the ones with their own photo, and for the others say where they are seen.
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
