import { useState } from "react";

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
