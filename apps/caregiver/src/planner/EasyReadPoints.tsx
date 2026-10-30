import { easyReadPoints } from "./format";

/** A step's easy-read wording as the traveller reads it: one bullet point per sentence. */
export function EasyReadPoints({ text, label }: { text: string | null | undefined; label?: string }) {
  const points = easyReadPoints(text);
  if (!points.length) return null;
  return (
    <ul className="rp__easy" aria-label={label}>
      {points.map((point, i) => <li key={i}>{point}</li>)}
    </ul>
  );
}
