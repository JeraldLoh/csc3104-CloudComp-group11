// A button that, given a `confirm` question, asks it in place before acting (with the answer buttons under it);
// without one it acts straight away. Used where a disruption makes the choice matter: starting or making a trip on
// a disrupted route, and sending a route with a free bridging bus.
import { useState, type ReactNode } from "react";

interface Props {
  /** Ask this first; nothing (or "") acts at once. */
  confirm?: ReactNode;
  /** The answer that goes ahead, e.g. "Yes, start it". */
  yes: string;
  onConfirm: () => void;
  className: string;
  disabled?: boolean;
  "aria-label"?: string;
  children: ReactNode;
}

export function ConfirmButton({ confirm, yes, onConfirm, className, disabled, children, ...rest }: Props) {
  const [asking, setAsking] = useState(false);
  if (asking) {
    return (
      <div className="rp__confirm rp__confirm--inline" role="group" aria-label="Are you sure?">
        {confirm}
        <div className="rp__row">
          <button type="button" className="rp__btn rp__btn--small" disabled={disabled}
            onClick={() => { setAsking(false); onConfirm(); }}>{yes}</button>
          <button type="button" className="rp__btn rp__btn--secondary rp__btn--small" onClick={() => setAsking(false)}>Cancel</button>
        </div>
      </div>
    );
  }
  return (
    <button type="button" className={className} disabled={disabled} aria-label={rest["aria-label"]}
      onClick={() => (confirm ? setAsking(true) : onConfirm())}>
      {children}
    </button>
  );
}
