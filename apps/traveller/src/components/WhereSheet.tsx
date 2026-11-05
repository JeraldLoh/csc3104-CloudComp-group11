import { useEffect, useRef } from "react";
import type { WhereAnswer } from "../lib/where";

/**
 * "Where are you?" from the caregiver (issue 5.5). Two big answers and nothing else to read: "I'm OK" tells the
 * caregiver where the traveller is; "I need help" does that and asks for help, as the Help button does.
 */
export function WhereSheet({ caregiver, onAnswer }: { caregiver: string; onAnswer: (answer: WhereAnswer) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => ref.current?.focus(), []);
  const who = caregiver.charAt(0).toUpperCase() + caregiver.slice(1); // "your caregiver" starts a sentence

  return (
    <div className="sheet" role="dialog" aria-modal="true" aria-labelledby="where-title" tabIndex={-1} ref={ref}>
      <div className="sheet__panel">
        <h2 id="where-title" className="sheet__title">{who} is asking where you are</h2>
        <p className="sheet__text">Are you OK?</p>
        <button className="btn btn--primary" onClick={() => onAnswer("ok")}>I'm OK</button>
        <button className="btn btn--help" onClick={() => onAnswer("help")}>I need help</button>
      </div>
    </div>
  );
}
