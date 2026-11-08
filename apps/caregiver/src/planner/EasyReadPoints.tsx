// Draws the easy-read points as a list, or nothing if there are none

import { easyReadPoints } from "./format";

export function EasyReadPoints({ text, label }: { text: string | null | undefined; label?: string }) {
  const points = easyReadPoints(text);
  if (!points.length) return null;
  return (
    <ul className="rp__easy" aria-label={label}>
      {points.map((point, i) => <li key={i}>{point}</li>)}
    </ul>
  );
}
