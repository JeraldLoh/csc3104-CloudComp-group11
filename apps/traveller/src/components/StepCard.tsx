import { useEffect, useRef, useState } from "react";
import { Pictogram } from "./Pictogram";
import { mediaUrl, type LoadedPack } from "../lib/pack";
import type { RoutePackStep } from "../types";

const MODE_NAME: Record<string, string> = { walk: "Walk", bus: "Bus", train: "Train" };

/** Reads the step aloud: the recorded audio if the pack has it, otherwise the phone's voice. */
function useListen(text: string, audioSrc: string | null) {
  const audio = useRef<HTMLAudioElement | null>(null);
  const [speaking, setSpeaking] = useState(false);
  useEffect(() => () => {
    audio.current?.pause();
    if ("speechSynthesis" in window) window.speechSynthesis.cancel();
  }, [text]);
  const play = () => {
    if (audioSrc) {
      audio.current ??= new Audio();
      audio.current.src = audioSrc;
      setSpeaking(true);
      audio.current.onended = audio.current.onerror = () => setSpeaking(false);
      void audio.current.play().catch(() => speak());
      return;
    }
    speak();
  };
  const speak = () => {
    if (!("speechSynthesis" in window)) return;
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 0.9;
    u.onend = u.onerror = () => setSpeaking(false);
    setSpeaking(true);
    window.speechSynthesis.speak(u);
  };
  const canListen = !!audioSrc || (typeof window !== "undefined" && "speechSynthesis" in window);
  return { play, speaking, canListen };
}

export function StepCard({ step, loaded, index, total }: { step: RoutePackStep; loaded: LoadedPack; index: number; total: number }) {
  const photo = mediaUrl(loaded, step.photoUrl);
  const [photoOk, setPhotoOk] = useState(true);
  useEffect(() => setPhotoOk(true), [photo]);
  const { play, speaking, canListen } = useListen(step.easyRead, mediaUrl(loaded, step.audioUrl));
  const mode = MODE_NAME[step.mode] ? step.mode : "walk";

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
        {photo && photoOk && <img className="sign__photo" src={photo} alt="" onError={() => setPhotoOk(false)} />}
        <p id="step-text" className="sign__text">{step.easyRead}</p>
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
