import { useEffect, useRef, useState } from "react";
import { Pictogram } from "./Pictogram";
import { mediaUrl, type LoadedPack } from "../lib/pack";
import { storedMediaUrl } from "../lib/offline";
import { easyReadPoints, landmarkCaption } from "../lib/easyRead";
import type { RoutePackStep } from "../types";

const MODE_NAME: Record<string, string> = { walk: "Walk", bus: "Bus", train: "Train" };

/** The stored copy of a photo/audio file if it's on the phone (works with no signal), else the network URL. */
function useStoredMedia(tripId: string, url: string | null, packVersion: string): string | null {
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    let objectUrl: string | null = null;
    setSrc(null);
    if (!url) return;
    storedMediaUrl(tripId, url, packVersion)
      .then((s) => {
        if (s.startsWith("blob:")) objectUrl = s;
        if (alive) setSrc(s);
        else if (objectUrl) URL.revokeObjectURL(objectUrl);
      })
      .catch(() => alive && setSrc(url));
    return () => {
      alive = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [tripId, url, packVersion]);
  return src;
}

/**
 * Reads the step aloud: the recorded audio if the pack has it, otherwise the phone's voice. `after` (the landmark
 * lines, which the recorded audio doesn't have) follows in the phone's voice.
 */
function useListen(text: string, audioSrc: string | null, after = "") {
  const audio = useRef<HTMLAudioElement | null>(null);
  const [speaking, setSpeaking] = useState(false);
  useEffect(() => () => {
    audio.current?.pause();
    if ("speechSynthesis" in window) window.speechSynthesis.cancel();
  }, [text, after]);
  const all = after ? `${text} ${after}` : text;
  const play = () => {
    if (audioSrc) {
      audio.current ??= new Audio();
      audio.current.src = audioSrc;
      setSpeaking(true);
      audio.current.onended = () => (after ? speak(after) : setSpeaking(false));
      audio.current.onerror = () => speak(all);          // the recording can't play: the phone reads it all
      void audio.current.play().catch(() => speak(all));
      return;
    }
    speak(all);
  };
  const speak = (words: string) => {
    if (!("speechSynthesis" in window)) return setSpeaking(false);
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(words);
    u.rate = 0.9;
    u.onend = u.onerror = () => setSpeaking(false);
    setSpeaking(true);
    window.speechSynthesis.speak(u);
  };
  const canListen = !!audioSrc || (typeof window !== "undefined" && "speechSynthesis" in window);
  return { play, speaking, canListen };
}

/** A photo from the pack: the stored copy if there is one; hidden if it can't be shown (the words still guide). */
function Photo({ tripId, loaded, path, className }: { tripId: string; loaded: LoadedPack; path: string | null; className: string }) {
  const src = useStoredMedia(tripId, mediaUrl(loaded, path), loaded.pack.packVersion);
  const [ok, setOk] = useState(true);
  useEffect(() => setOk(true), [src]);
  return src && ok ? <img className={className} src={src} alt="" onError={() => setOk(false)} /> : null;
}

export function StepCard({ step, loaded, index, total, tripId }: { step: RoutePackStep; loaded: LoadedPack; index: number; total: number; tripId: string }) {
  const audio = useStoredMedia(tripId, mediaUrl(loaded, step.audioUrl), loaded.pack.packVersion);
  const landmarks = step.landmarks ?? [];
  // The landmark lines as shown, read after the step: with a photo first, then those without, as on screen
  const landmarkLines = [...landmarks.filter((m) => m.photoUrl), ...landmarks.filter((m) => !m.photoUrl)]
    .map((m) => landmarkCaption(m, !!step.photoUrl))
    .map((line) => (/[.!?]$/.test(line) ? line : `${line}.`))
    .join(" ");
  const { play, speaking, canListen } = useListen(step.easyRead, audio, landmarkLines);
  const mode = MODE_NAME[step.mode] ? step.mode : "walk";
  const points = easyReadPoints(step.easyRead);

  return (
    <article className={`sign sign--${mode}`} aria-labelledby="step-text">
      <header className="sign__band">
        <Pictogram mode={mode} />
        <span className="sign__mode">{MODE_NAME[mode]}</span>
        <span className="sign__count">Step {index + 1} of {total}</span>
      </header>
      <ol className="sign__progress" aria-hidden>
        {Array.from({ length: total }, (_, i) => (
          <li key={i} className={i < index ? "done" : i === index ? "now" : ""} />
        ))}
      </ol>
      <div className="sign__body">
        <Photo tripId={tripId} loaded={loaded} path={step.photoUrl} className="sign__photo" />
        {/* One action per line, the same points the caregiver saw in the editor. A single sentence stays a sentence. */}
        {points.length > 1 ? (
          <ul id="step-text" className="sign__text sign__points">
            {points.map((p, i) => <li key={i}>{p}</li>)}
          </ul>
        ) : (
          <p id="step-text" className="sign__text">{step.easyRead}</p>
        )}
        {/* Landmarks with a photo side by side; one already shown in another photo gets a line saying which. */}
        {landmarks.some((m) => m.photoUrl) && (
          <ul className="sign__landmarks" aria-label="Things to look out for">
            {landmarks.filter((m) => m.photoUrl).map((m, i) => (
              <li key={`${m.name}-${i}`}>
                <figure className="sign__landmark">
                  <Photo tripId={tripId} loaded={loaded} path={m.photoUrl} className="sign__landmark-photo" />
                  <figcaption>{landmarkCaption(m, !!step.photoUrl)}</figcaption>
                </figure>
              </li>
            ))}
          </ul>
        )}
        {landmarks.filter((m) => !m.photoUrl).map((m, i) => (
          <p key={`${m.name}-${i}`} className="sign__landmark-note">{landmarkCaption(m, !!step.photoUrl)}</p>
        ))}
        {step.backup && <p className="sign__backup">{step.backup}</p>}
        {canListen && (
          <button className="btn btn--quiet" onClick={play} aria-pressed={speaking}>
            <SpeakerIcon /> {speaking ? "Listening…" : "Listen"}
          </button>
        )}
      </div>
    </article>
  );
}

function SpeakerIcon() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 9h4l5-4v14l-5-4H4z" fill="currentColor" />
      <path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12" />
    </svg>
  );
}
