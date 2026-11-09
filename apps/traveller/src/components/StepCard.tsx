import { useEffect, useRef, useState } from "react";
import { Pictogram } from "./Pictogram";
import { mediaUrl, type LoadedPack } from "../lib/pack";
import { storedMediaUrl } from "../lib/offline";
import { easyReadPoints, landmarkCaption } from "../lib/easyRead";
import type { Guide } from "../app/useGuide";
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
  const mine = useRef(false);            // is the phone's voice saying our words (not a prompt the guide read out)?
  useEffect(() => () => {
    audio.current?.pause();
    if (mine.current && "speechSynthesis" in window) window.speechSynthesis.cancel();
    mine.current = false;
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
    u.onend = u.onerror = () => {
      mine.current = false;
      setSpeaking(false);
    };
    mine.current = true;
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

const withStop = (line: string) => (/[.!?]$/.test(line) ? line : `${line}.`);

/**
 * One step of the trip. With a `guide` that is on (src/lib/guide.ts, while travelling with GPS), it shows one thing
 * at a time: a big prompt ("5 more stops", "Press the bell now"), only the lines that matter now, and each landmark's
 * photo as the traveller gets near it; "Show all" shows the whole step. Without one, the whole step, as before.
 */
export function StepCard({ step, loaded, index, total, tripId, guide }: {
  step: RoutePackStep; loaded: LoadedPack; index: number; total: number; tripId: string; guide?: Guide | null;
}) {
  const [showAll, setShowAll] = useState(false);
  useEffect(() => setShowAll(false), [index, loaded.pack.packVersion]);
  const guided = !!guide?.on && !showAll;

  const audio = useStoredMedia(tripId, mediaUrl(loaded, step.audioUrl), loaded.pack.packVersion);
  const landmarks = (step.landmarks ?? []).map((m, i) => ({ m, i }))
    .filter(({ i }) => !guided || (guide?.landmarkShown[i] ?? true));
  const withPhoto = landmarks.filter(({ m }) => m.photoUrl);
  const withoutPhoto = landmarks.filter(({ m }) => !m.photoUrl);
  const points = guided ? guide!.lines.map((l) => l.text) : easyReadPoints(step.easyRead);
  // The landmark lines as shown, read after the step: with a photo first, then those without, as on screen. When
  // guided, a landmark the shown lines already name ("Look out for Oasis Terraces on your right") isn't read twice.
  const named = (name: string) => points.some((p) => p.toLowerCase().includes(name.toLowerCase()));
  const landmarkLines = [...withPhoto, ...withoutPhoto]
    .filter(({ m }) => !guided || !named(m.name))
    .map(({ m }) => withStop(landmarkCaption(m, !!step.photoUrl)))
    .join(" ");
  // Listen reads what is on screen: guided, the prompt and the lines shown now, in the phone's voice (the recording
  // is of the whole step); otherwise the whole step, recorded if the pack has it.
  const guidedText = [guide?.prompt?.say, ...points.map(withStop)].filter(Boolean).join(" ");
  const { play, speaking, canListen } = useListen(guided ? guidedText : step.easyRead, guided ? null : audio, landmarkLines);
  const mode = MODE_NAME[step.mode] ? step.mode : "walk";
  const showBackup = step.backup && (!guided || !guide!.started);
  // Near a landmark, its photo is what to look for now: it takes the step photo's place until it's passed
  const stepPhoto = !guided || !withPhoto.some(({ i }) => guide!.near.includes(i));

  return (
    <article className={`sign sign--${mode}${guided ? " sign--guided" : ""}`} aria-labelledby="step-now step-text">
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
        {/* The live prompt: kept on the page while guided so screen readers announce each change */}
        {guide?.on && (
          <p id="step-now" className="sign__now" role="status" aria-live="assertive">
            {guide.prompt && <span key={guide.prompt.key}>{guide.prompt.title}</span>}
          </p>
        )}
        {stepPhoto && <Photo tripId={tripId} loaded={loaded} path={step.photoUrl} className="sign__photo" />}
        {/* One action per line, the same points the caregiver saw in the editor. A single sentence stays a sentence. */}
        {points.length > 1 ? (
          <ul id="step-text" className="sign__text sign__points">
            {points.map((p, i) => <li key={`${p}-${i}`}>{p}</li>)}
          </ul>
        ) : points.length === 1 ? (
          <p id="step-text" className="sign__text">{guided ? points[0] : step.easyRead}</p>
        ) : null}
        {/* Landmarks with a photo side by side; one already shown in another photo gets a line saying which. */}
        {withPhoto.length > 0 && (
          <ul className={`sign__landmarks${guided ? " sign__landmarks--guided" : ""}`} aria-label="Things to look out for">
            {withPhoto.map(({ m, i }) => (
              <li key={`${m.name}-${i}`}>
                <figure className="sign__landmark">
                  <Photo tripId={tripId} loaded={loaded} path={m.photoUrl} className="sign__landmark-photo" />
                  <figcaption>{landmarkCaption(m, !!step.photoUrl)}</figcaption>
                </figure>
              </li>
            ))}
          </ul>
        )}
        {/* Guided, a note for a landmark the lines already name ("Get off at Block 221") would say it twice */}
        {withoutPhoto.filter(({ m }) => !guided || !named(m.name)).map(({ m, i }) => (
          <p key={`${m.name}-${i}`} className="sign__landmark-note">{landmarkCaption(m, !!step.photoUrl)}</p>
        ))}
        {showBackup && <p className="sign__backup">{step.backup}</p>}
        {guide?.on && (guide.hidden > 0 || showAll) && (
          <button className="btn btn--quiet sign__more" onClick={() => setShowAll((v) => !v)} aria-expanded={showAll}>
            {showAll ? "Show less" : `Show all instructions (${guide.hidden} more)`}
          </button>
        )}
        <div className="sign__tools">
          {canListen && (
            <button className="btn btn--quiet" onClick={play} aria-pressed={speaking}>
              <SpeakerIcon /> {speaking ? "Listening…" : "Listen"}
            </button>
          )}
          {guide?.on && (
            <button className="btn btn--quiet" onClick={() => guide.setReadOut(!guide.readOut)} aria-pressed={guide.readOut}
              aria-label={`Read aloud as I go: ${guide.readOut ? "on" : "off"}`}>
              Read aloud: {guide.readOut ? "on" : "off"}
            </button>
          )}
        </div>
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
