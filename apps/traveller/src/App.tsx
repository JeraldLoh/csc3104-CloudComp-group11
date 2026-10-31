import { useEffect, useMemo } from "react";
import { useCareRoute } from "./app/useCareRoute";
import { readFlags, readSession } from "./app/session";
import { ConnectionStatus } from "./components/ConnectionStatus";
import { StepCard } from "./components/StepCard";
import { HelpSheet } from "./components/HelpSheet";
import { DebugPanel } from "./components/DebugPanel";
import type { Session } from "./types";

export default function App() {
  const flags = useMemo(() => readFlags(), []);
  const session = useMemo(() => readSession(), []);
  if (!session) {
    return (
      <Screen>
        <div className="message">
          <h1>Open your trip link</h1>
          <p>Ask your caregiver to send you the link for your trip.</p>
        </div>
      </Screen>
    );
  }
  return <Journey session={session} demo={flags.demo} debug={flags.debug} />;
}

function Screen({ children, footer }: { children: React.ReactNode; footer?: React.ReactNode }) {
  return (
    <div className="app">
      <main className="app__main">{children}</main>
      {footer}
    </div>
  );
}

function Journey({ session, demo, debug }: { session: Session; demo: boolean; debug: boolean }) {
  const app = useCareRoute(session, { demo });
  // When the route changes mid-trip, bring the "Your trip has changed" notice and the
  // new instruction into view, even if the traveller had scrolled down.
  useEffect(() => {
    if (app.routeChangedAt !== null) document.querySelector(".app__main")?.scrollTo({ top: 0, behavior: "smooth" });
  }, [app.routeChangedAt]);
  const pack = app.loaded?.pack;
  const caregiver = pack?.caregiver.name ?? "your caregiver";
  const debugPanel = debug ? <DebugPanel app={app} /> : null;

  if (app.phase === "loading") {
    return <Screen><div className="message"><p className="lead">Getting your trip ready…</p></div></Screen>;
  }

  if (app.phase === "no-pack" || !pack || !app.loaded) {
    return (
      <Screen>
        <div className="message">
          <h1>Your trip isn't ready yet</h1>
          <p>Ask {caregiver} to check your trip, then try again.</p>
          <button className="btn btn--primary" onClick={() => location.reload()}>Try again</button>
        </div>
        {debugPanel}
      </Screen>
    );
  }

  if (app.phase === "ready") {
    const first = pack.steps[0];
    return (
      <Screen>
        <div className="message">
          <h1>{pack.travellerName ? `Hi ${pack.travellerName}` : "Hi there"}</h1>
          <p className="lead">Your trip has {pack.steps.length} steps.</p>
          {first && <p>First: {first.easyRead}</p>}
          <p className="note">While you travel, {caregiver} can see where you are. Your phone will ask to use your location.</p>
          <OfflineReadiness app={app} />
          <button className="btn btn--primary" onClick={() => void app.start()}>Start my trip</button>
        </div>
        {debugPanel}
      </Screen>
    );
  }

  if (app.phase === "arrived") {
    // Only say the caregiver knows once the "arrived" message has actually got through.
    const told = !(app.requests?.pending.some((r) => r.label === "arrived") ?? true);
    return (
      <Screen>
        <div className="message message--arrived">
          <Tick />
          <h1>You made it</h1>
          <p className="lead">
            {told ? `${caregiver} knows you have arrived.` : `We are telling ${caregiver} you have arrived.`}
          </p>
        </div>
        {debugPanel}
      </Screen>
    );
  }

  const step = pack.steps[app.stepIndex] ?? pack.steps[0];
  const isLast = app.stepIndex >= pack.steps.length - 1;

  return (
    <Screen
      footer={
        <footer className="app__footer">
          <button className="btn btn--help" onClick={() => void app.askForHelp()}>
            <HelpIcon /> Help
          </button>
        </footer>
      }
    >
      <ConnectionStatus state={app.sender} offline={app.offlineSim || !navigator.onLine} caregiver={caregiver} />
      {app.problem === "location-denied" && (
        <p className="banner" role="alert">Turn on location for this app so {caregiver} can help you.</p>
      )}
      {app.problem === "location-unavailable" && (
        <p className="banner" role="alert">Your phone can't find where you are. Keep going and follow the steps.</p>
      )}
      {app.routeChangedAt !== null && (
        <div className="banner banner--changed" role="alert">
          <p>Your trip has changed. Read this step again.</p>
          <button className="btn btn--quiet" onClick={app.dismissRouteChange}>OK</button>
        </div>
      )}
      <StepCard step={step} loaded={app.loaded} index={app.stepIndex} total={pack.steps.length} tripId={session.tripId} />
      <div className="actions">
        <button className="btn btn--primary" onClick={() => void (isLast ? app.finish() : app.next())}>
          {isLast ? "I've arrived" : "I've done this step"}
        </button>
        {app.stepIndex > 0 && (
          <button className="btn btn--quiet" onClick={() => void app.back()}>Go back a step</button>
        )}
      </div>
      {debugPanel}
      {app.helpAskedAt !== null && (
        <HelpSheet askedAt={app.helpAskedAt} requests={app.requests} caregiver={caregiver} phone={pack.caregiver.phone} onClose={app.closeHelp} />
      )}
    </Screen>
  );
}

/**
 * Before setting off: is the trip on the phone, so it keeps working with no signal?
 * Never blocks starting; the steps are text and work regardless.
 */
function OfflineReadiness({ app }: { app: ReturnType<typeof useCareRoute> }) {
  const m = app.offline.media;
  if (!m || !m.finished) {
    return (
      <p className="ready ready--working" role="status">
        Saving your trip to this phone{m && m.total > 0 ? ` (${m.done} of ${m.total} pictures)` : ""}…
      </p>
    );
  }
  if (m.failed.length > 0) {
    return (
      <p className="ready ready--partial" role="status">
        Some pictures didn't save. Your steps will still work without signal.
      </p>
    );
  }
  return (
    <p className="ready ready--done" role="status">
      <Tick size={22} /> Your trip is saved. It works without signal.
    </p>
  );
}

function HelpIcon() {
  return (
    <svg width="28" height="28" viewBox="0 0 24 24" aria-hidden fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round">
      <circle cx="12" cy="12" r="9.5" />
      <path d="M9.4 9.4a2.7 2.7 0 1 1 3.8 2.5c-.8.4-1.2 1-1.2 1.9v.5" />
      <circle cx="12" cy="17.4" r=".6" fill="currentColor" />
    </svg>
  );
}

function Tick({ size = 88 }: { size?: number }) {
  return (
    <svg className="tick" width={size} height={size} viewBox="0 0 48 48" aria-hidden>
      <circle cx="24" cy="24" r="22" fill="currentColor" />
      <path d="M14 25l7 7 13-15" fill="none" stroke="#fff" strokeWidth="4.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
