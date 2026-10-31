// Preview of a step as it appears on the traveller's phone, with the mode pictogram, photo, and text.
import { createContext, useContext, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { StepMode } from "../journey/types";

export const PreviewSlot = createContext<HTMLElement | null>(null);

const MODE_NAME: Record<StepMode, string> = { walk: "Walk", bus: "Bus", train: "Train" };

interface Props {
  mode: StepMode;
  text: string;
  photoUrl: string | null;
  backup?: string | null;
  index: number;
  total: number;
  onPrev: () => void;
  onNext: () => void;
  onHide: () => void;
}

export function PhonePreview({ mode, text, photoUrl, backup, index, total, onPrev, onNext, onHide }: Props) {
  const slot = useContext(PreviewSlot);
  const last = index === total - 1;
  const preview: ReactNode = (
    <aside className="rp__preview" aria-label={`How step ${index + 1} looks on the traveller's phone`}>
      <div className="rp__phone">
        <div className="rp__phone-screen">
          <article className={`tv-sign tv-sign--${mode}`}>
            <header className="tv-sign__band">
              <Pictogram mode={mode} />
              <span className="tv-sign__mode">{MODE_NAME[mode]}</span>
              <span className="tv-sign__count">Step {index + 1} of {total}</span>
            </header>
            <ol className="tv-sign__progress" aria-hidden>
              {Array.from({ length: total }, (_, i) => <li key={i} className={i < index ? "done" : i === index ? "now" : ""} />)}
            </ol>
            <div className="tv-sign__body">
              {photoUrl && <img className="tv-sign__photo" src={photoUrl} alt="" />}
              {text
                ? <p className="tv-sign__text">{text}</p>
                : <p className="tv-sign__text tv-sign__text--auto">The automatic wording is used when you save.</p>}
              {backup && <p className="tv-sign__backup">{backup}</p>}
              <span className="tv-btn tv-btn--quiet">Listen</span>
            </div>
          </article>
          <span className="tv-btn tv-btn--primary">{last ? "I've arrived" : "I've done this step"}</span>
          <div className="rp__phone-help"><span className="tv-btn tv-btn--help">Help</span></div>
        </div>
      </div>
      <div className="rp__preview-nav">
        <button type="button" className="rp__btn rp__btn--secondary rp__btn--small" onClick={onPrev} disabled={index === 0}
          aria-label="Preview the step before">‹</button>
        <span className="rp__meta">{`Step ${index + 1} of ${total}`}</span>
        <button type="button" className="rp__btn rp__btn--secondary rp__btn--small" onClick={onNext} disabled={last}
          aria-label="Preview the next step">›</button>
        <button type="button" className="rp__btn rp__btn--secondary rp__btn--small" onClick={onHide}>Hide</button>
      </div>
    </aside>
  );
  return slot ? createPortal(preview, slot) : preview;
}

function Pictogram({ mode }: { mode: StepMode }) {
  const common = { width: 30, height: 30, viewBox: "0 0 48 48", "aria-hidden": true, fill: "currentColor" } as const;
  if (mode === "bus") {
    return (
      <svg {...common}>
        <rect x="9" y="6" width="30" height="31" rx="5" />
        <rect x="13" y="11" width="22" height="11" rx="1.5" fill="var(--tv-band)" />
        <circle cx="16" cy="30" r="2.5" fill="var(--tv-band)" />
        <circle cx="32" cy="30" r="2.5" fill="var(--tv-band)" />
        <rect x="12" y="36" width="5" height="6" rx="1.5" />
        <rect x="31" y="36" width="5" height="6" rx="1.5" />
      </svg>
    );
  }
  if (mode === "train") {
    return (
      <svg {...common}>
        <rect x="11" y="5" width="26" height="31" rx="7" />
        <rect x="15" y="10" width="18" height="10" rx="1.5" fill="var(--tv-band)" />
        <circle cx="18" cy="28" r="2.5" fill="var(--tv-band)" />
        <circle cx="30" cy="28" r="2.5" fill="var(--tv-band)" />
        <path d="M15 43l5-7h2l-4 7zM33 43l-5-7h-2l4 7z" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <circle cx="26" cy="7" r="4.5" />
      <path d="M23 14c2-1 5-1 6.5.6l4.8 6.2 5.6 1.8-1.2 3.6-6.8-2.3-2.4-3-1.6 7.4 5 5.8 1.5 10.4-4 .6-1.3-9-5.2-5.4-3.2 14.8H16.8l4.4-21.6-3.6 1.6-2.6 6-3.6-1.6 3.4-7.6z" />
    </svg>
  );
}
