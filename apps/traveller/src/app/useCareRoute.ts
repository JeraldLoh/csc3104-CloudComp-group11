// Wires the pieces together for the screens. The screens only read state and call actions;
// all the reliability logic lives in src/lib and is tested on its own.
import { useCallback, useEffect, useRef, useState } from "react";
import { getMeta, metaKeys, openCareRouteDB, setMeta, type DB } from "../lib/db";
import { Outbox } from "../lib/outbox";
import { Sender, type SenderState } from "../lib/sender";
import { RequestQueue, type RequestQueueState } from "../lib/requests";
import { RECONNECT_JITTER_MS, Tracker, type Fix, type TrackerProblem } from "../lib/tracker";
import { HttpTransport } from "../lib/api";
import { isOnline, netsim, withNetsim } from "../lib/netsim";
import { checkForNewPack, loadPack, type LoadedPack } from "../lib/pack";
import { DEMO_PACK, DEMO_PACK_CHANGED, DemoWalker } from "../lib/demo";
import { ScreenWakeLock, type WakeLockState } from "../lib/wakelock";
import { batteryPct, startBatteryWatch } from "../lib/battery";
import { downloadMedia, forgetTrip, requestPersistentStorage, type DownloadProgress } from "../lib/offline";
import { appShell } from "../lib/sw-register";
import type { Session } from "../types";

export type Phase = "loading" | "no-pack" | "ready" | "travelling" | "arrived";

interface Engine {
  db: DB;
  outbox: Outbox;
  sender: Sender;
  requests: RequestQueue;
  tracker: Tracker;
  wake: ScreenWakeLock;
  applyPack: (p: LoadedPack, announce: boolean) => void;
}

/** How often to ask whether the caregiver has changed the route. */
const UPDATE_CHECK_MS = 60_000;

export interface CareRoute {
  phase: Phase;
  loaded: LoadedPack | null;
  stepIndex: number;
  sender: SenderState | null;
  requests: RequestQueueState | null;
  problem: TrackerProblem;
  fix: Fix | null;
  helpAskedAt: number | null;
  offlineSim: boolean;
  outboxDropped: number;
  /** Is everything needed for this trip stored on the phone? */
  offline: { appShell: boolean; media: DownloadProgress | null; persisted: boolean | null };
  /** Set when a newer version of the route arrived during the trip. */
  routeChangedAt: number | null;
  dismissRouteChange(): void;
  wakeLock: WakeLockState;
  /** Demo only: pretend the caregiver changed the route. */
  simulateRouteChange(): void;
  /** Demo only: forget this trip on this phone and start again from "Hi Alex". */
  resetDemo(): Promise<void>;
  start(): Promise<void>;
  next(): Promise<void>;
  back(): Promise<void>;
  askForHelp(): Promise<void>;
  closeHelp(): void;
  finish(): Promise<void>;
  setOfflineSim(on: boolean): void;
  checkInNow(): Promise<void>;
}

const DEMO_TICK_MS = 3000;

export function useCareRoute(session: Session, opts: { demo: boolean }): CareRoute {
  const engine = useRef<Engine | null>(null);
  const stepRef = useRef(0);
  const demoTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  const [phase, setPhase] = useState<Phase>("loading");
  const [loaded, setLoaded] = useState<LoadedPack | null>(null);
  const [stepIndex, setStepIndex] = useState(0);
  const [sender, setSender] = useState<SenderState | null>(null);
  const [requests, setRequests] = useState<RequestQueueState | null>(null);
  const [problem, setProblem] = useState<TrackerProblem>(null);
  const [fix, setFix] = useState<Fix | null>(null);
  const [helpAskedAt, setHelpAskedAt] = useState<number | null>(null);
  const [offlineSim, setOfflineSimState] = useState(netsim.offline);
  const [media, setMedia] = useState<DownloadProgress | null>(null);
  const [shellReady, setShellReady] = useState(appShell.ready);
  const [persisted, setPersisted] = useState<boolean | null>(null);
  const [routeChangedAt, setRouteChangedAt] = useState<number | null>(null);
  const [wakeLock, setWakeLock] = useState<WakeLockState>("off");

  const startTracking = useCallback((e: Engine, pack: LoadedPack) => {
    e.tracker.start();
    void e.wake.enable();
    if (opts.demo) {
      const walker = new DemoWalker(pack.pack.path);
      e.tracker.acceptFix(walker.next(Date.now()));
      demoTimer.current = setInterval(() => e.tracker.acceptFix(walker.next(Date.now())), DEMO_TICK_MS);
    }
  }, [opts.demo]);

  const stopTracking = useCallback((e: Engine) => {
    e.tracker.stop();
    void e.wake.disable();
    if (demoTimer.current) clearInterval(demoTimer.current);
    demoTimer.current = null;
  }, []);

  useEffect(() => {
    let disposed = false;
    const cleanups: (() => void)[] = [];
    (async () => {
      const db = await openCareRouteDB();
      if (disposed) return db.close();
      startBatteryWatch();
      const transport = withNetsim(new HttpTransport());
      const outbox = new Outbox(db);
      const sender = new Sender({ outbox, transport, isOnline });
      const requests = new RequestQueue(db, transport);
      const packRef = { routeVersion: null as number | null };
      const tracker = new Tracker({
        session, outbox, sender,
        geolocation: opts.demo ? null : navigator.geolocation ?? null,
        getProgress: () => ({ stepIndex: stepRef.current, routeVersion: packRef.routeVersion }),
        getBatteryPct: batteryPct,
        isOnline,
        onProblem: (p) => setProblem(opts.demo && p === "location-unavailable" ? null : p),
        onFix: setFix,
      });
      const wake = new ScreenWakeLock(undefined, (st) => !disposed && setWakeLock(st));
      cleanups.push(sender.subscribe(setSender), requests.subscribe(setRequests));
      cleanups.push(netsim.onChange((off) => {
        setOfflineSimState(off);
        if (!off) {
          void sender.retryNow({ jitterMs: RECONNECT_JITTER_MS });
          void requests.retryNow(); // help etc. go straight away
        }
      }));
      cleanups.push(appShell.onChange(setShellReady));
      // Download photos/audio; called again when signal returns (fetching only what's
      // missing) and when a new route version arrives. If a download is already running,
      // the newest request is remembered and done straight after it.
      let mediaRunning = false;
      let mediaWanted: LoadedPack | null = null;
      let mediaDoneFor: string | null = null; // packVersion fully downloaded
      const fetchMedia = async (p: LoadedPack) => {
        mediaWanted = p;
        if (mediaRunning) return;
        mediaRunning = true;
        try {
          while (mediaWanted && !disposed) {
            const target: LoadedPack = mediaWanted;
            mediaWanted = null;
            if (mediaDoneFor === target.pack.packVersion) continue;
            const r = await downloadMedia(session.tripId, target.pack, target.baseUrl, (m) => !disposed && setMedia(m));
            if (r.failed.length === 0) mediaDoneFor = target.pack.packVersion;
          }
        } finally {
          mediaRunning = false;
        }
      };

      let currentPack: LoadedPack | null = null;
      /** Switch to a (new) version of the route. */
      const applyPack = (p: LoadedPack, announce: boolean) => {
        currentPack = p;
        packRef.routeVersion = p.pack.routeVersion; // check-ins now report the new version
        setLoaded(p);
        const last = p.pack.steps.length - 1;
        if (stepRef.current > last) {
          stepRef.current = last;
          setStepIndex(last);
        }
        void fetchMedia(p);
        if (announce) {
          setRouteChangedAt(Date.now());
          void tracker.checkInNow(); // detectors should judge against the new route at once
        }
      };

      // Has the caregiver changed the route? Cheap check every minute, and straight away
      // when signal comes back or the traveller returns to the app.
      let checking = false;
      const checkForUpdate = async () => {
        if (opts.demo || checking || !currentPack || disposed) return;
        checking = true;
        try {
          const newer = await checkForNewPack(db, session.tripId, currentPack.pack.packVersion);
          if (newer && !disposed) applyPack(newer, true);
        } finally {
          checking = false;
        }
      };
      const updateTimer = setInterval(() => void checkForUpdate(), UPDATE_CHECK_MS);
      cleanups.push(() => clearInterval(updateTimer));

      const signalBack = () => {
        void requests.retryNow();
        if (currentPack) void fetchMedia(currentPack);
        void checkForUpdate();
      };
      cleanups.push(netsim.onChange((off) => !off && signalBack()));
      window.addEventListener("online", signalBack);
      cleanups.push(() => window.removeEventListener("online", signalBack));
      const onShown = () => document.visibilityState === "visible" && void checkForUpdate();
      document.addEventListener("visibilitychange", onShown);
      cleanups.push(() => document.removeEventListener("visibilitychange", onShown));

      const e: Engine = { db, outbox, sender, requests, tracker, wake, applyPack };
      engine.current = e;

      await setMeta(db, metaKeys.session, session);
      const pack = await loadPack(db, session.tripId, { demo: opts.demo ? DEMO_PACK : undefined });
      if (disposed) return;
      if (!pack) return setPhase("no-pack");
      applyPack(pack, false);
      void requestPersistentStorage().then((ok) => !disposed && setPersisted(ok));

      const savedStep = (await getMeta<number>(db, metaKeys.stepIndex(session.tripId))) ?? 0;
      const started = await getMeta<"travelling" | "arrived">(db, metaKeys.tripStarted(session.tripId));
      stepRef.current = savedStep;
      setStepIndex(savedStep);
      // Anything left from before a reload goes out now.
      void sender.flush();
      void requests.flush();
      if (started === "travelling") {
        setPhase("travelling");
        startTracking(e, pack); // reopened mid-journey: carry on without asking again
      } else {
        setPhase(started === "arrived" ? "arrived" : "ready");
      }
    })();
    return () => {
      disposed = true;
      const e = engine.current;
      if (e) {
        stopTracking(e);
        e.sender.dispose();
        e.requests.dispose();
        e.db.close();
      }
      cleanups.forEach((c) => c());
      engine.current = null;
    };
  }, [session, opts.demo, startTracking, stopTracking]);

  const goTo = useCallback(async (i: number) => {
    const e = engine.current;
    if (!e) return;
    stepRef.current = i;
    setStepIndex(i);
    await setMeta(e.db, metaKeys.stepIndex(session.tripId), i);
    await e.tracker.checkInNow(); // tell the backend straight away, not at the next tick
  }, [session.tripId]);

  const start = useCallback(async () => {
    const e = engine.current;
    if (!e || !loaded) return;
    await setMeta(e.db, metaKeys.tripStarted(session.tripId), "travelling");
    setPhase("travelling");
    void e.requests.add("trip start", `/api/trips/${encodeURIComponent(session.tripId)}/start`, {});
    startTracking(e, loaded);
  }, [loaded, session.tripId, startTracking]);

  const next = useCallback(async () => {
    if (!loaded) return;
    await goTo(Math.min(stepRef.current + 1, loaded.pack.steps.length - 1));
  }, [goTo, loaded]);

  const back = useCallback(async () => {
    await goTo(Math.max(stepRef.current - 1, 0));
  }, [goTo]);

  const askForHelp = useCallback(async () => {
    const e = engine.current;
    if (!e) return;
    setHelpAskedAt(Date.now());
    const f = e.tracker.fix;
    // Queued, so it keeps trying with no signal. The Alert Service turns repeats into
    // one incident (U-042#T-001#help_requested#<step>), so retrying is safe.
    // travellerId and stepIndex go too: Help can be pressed before any check-in has reached the servers.
    void e.requests.add("help", "/api/help", {
      tripId: session.tripId, travellerId: session.travellerId, stepIndex: stepRef.current,
      lat: f?.lat ?? null, lng: f?.lng ?? null,
    });
    void e.tracker.checkInNow();
  }, [session.tripId, session.travellerId]);

  const finish = useCallback(async () => {
    const e = engine.current;
    if (!e) return;
    await e.tracker.checkInNow();
    stopTracking(e);
    await setMeta(e.db, metaKeys.tripStarted(session.tripId), "arrived");
    setPhase("arrived");
    // The photos aren't needed any more; free the space (and don't keep pictures of the
    // traveller's route on the phone longer than necessary).
    void forgetTrip(session.tripId);
    // Queued like Help, so it keeps trying with no signal. Repeats only tell the caregiver once.
    void e.requests.add("arrived", "/api/arrived", { tripId: session.tripId, travellerId: session.travellerId });
    void e.requests.add("trip end", `/api/trips/${encodeURIComponent(session.tripId)}/end`, {});
  }, [session.tripId, session.travellerId, stopTracking]);

  return {
    phase, loaded, stepIndex, sender, requests, problem, fix, helpAskedAt, offlineSim,
    outboxDropped: engine.current?.outbox.dropped ?? 0,
    offline: { appShell: shellReady, media, persisted },
    routeChangedAt,
    dismissRouteChange: () => setRouteChangedAt(null),
    wakeLock,
    resetDemo: async () => {
      const e = engine.current;
      if (!opts.demo || !e) return; // never wipe a real trip (it may hold unsent check-ins)
      stopTracking(e);
      const tx = e.db.transaction(["outbox", "requests", "meta"], "readwrite");
      await Promise.all([tx.objectStore("outbox").clear(), tx.objectStore("requests").clear(), tx.objectStore("meta").clear()]);
      await tx.done;
      await forgetTrip(session.tripId);
      location.reload();
    },
    simulateRouteChange: () => {
      const e = engine.current;
      if (!opts.demo || !e || !loaded) return; // never replace a real trip's route
      e.applyPack({ ...loaded, pack: DEMO_PACK_CHANGED, source: "network" }, true);
    },
    start, next, back, askForHelp, finish,
    closeHelp: () => setHelpAskedAt(null),
    setOfflineSim: (on) => netsim.set(on),
    checkInNow: async () => {
      await engine.current?.tracker.checkInNow();
    },
  };
}
