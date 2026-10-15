// Wires the pieces together for the screens. The screens only read state and call actions;
// all the reliability logic lives in src/lib and is tested on its own.
import { useCallback, useEffect, useRef, useState } from "react";
import { getMeta, metaKeys, openCareRouteDB, setMeta, type DB } from "../lib/db";
import { Outbox } from "../lib/outbox";
import { Sender, type SenderState } from "../lib/sender";
import { RequestQueue, type RequestQueueState } from "../lib/requests";
import { Tracker, type Fix, type TrackerProblem } from "../lib/tracker";
import { HttpTransport } from "../lib/api";
import { isOnline, netsim, withNetsim } from "../lib/netsim";
import { loadPack, type LoadedPack } from "../lib/pack";
import { DEMO_PACK, DemoWalker } from "../lib/demo";
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
}

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

  const startTracking = useCallback((e: Engine, pack: LoadedPack) => {
    e.tracker.start();
    if (opts.demo) {
      const walker = new DemoWalker(pack.pack.path);
      e.tracker.acceptFix(walker.next(Date.now()));
      demoTimer.current = setInterval(() => e.tracker.acceptFix(walker.next(Date.now())), DEMO_TICK_MS);
    }
  }, [opts.demo]);

  const stopTracking = useCallback((e: Engine) => {
    e.tracker.stop();
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
      const e: Engine = { db, outbox, sender, requests, tracker };
      engine.current = e;
      cleanups.push(sender.subscribe(setSender), requests.subscribe(setRequests));
      cleanups.push(netsim.onChange((off) => {
        setOfflineSimState(off);
        if (!off) {
          void sender.retryNow();
          void requests.retryNow();
        }
      }));
      cleanups.push(appShell.onChange(setShellReady));
      // Download photos/audio; called again when signal returns, fetching only what's missing.
      let downloading = false;
      let lastMedia: DownloadProgress | null = null;
      const fetchMedia = async (p: LoadedPack) => {
        if (downloading || disposed) return;
        if (lastMedia && lastMedia.finished && lastMedia.failed.length === 0) return;
        downloading = true;
        try {
          lastMedia = await downloadMedia(session.tripId, p.pack, p.baseUrl, (m) => !disposed && setMedia(m));
        } finally {
          downloading = false;
        }
      };
      let currentPack: LoadedPack | null = null;
      const retryMedia = () => currentPack && void fetchMedia(currentPack);
      cleanups.push(netsim.onChange((off) => !off && retryMedia()));
      const onOnline = () => {
        void requests.retryNow();
        retryMedia();
      };
      window.addEventListener("online", onOnline);
      cleanups.push(() => window.removeEventListener("online", onOnline));

      await setMeta(db, metaKeys.session, session);
      const pack = await loadPack(db, session.tripId, { demo: opts.demo ? DEMO_PACK : undefined });
      if (disposed) return;
      if (!pack) return setPhase("no-pack");
      packRef.routeVersion = pack.pack.routeVersion;
      setLoaded(pack);
      currentPack = pack;
      void requestPersistentStorage().then((ok) => !disposed && setPersisted(ok));
      void fetchMedia(pack);

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
    void e.requests.add("help", "/api/help", { tripId: session.tripId, lat: f?.lat ?? null, lng: f?.lng ?? null });
    void e.tracker.checkInNow();
  }, [session.tripId]);

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
    void e.requests.add("trip end", `/api/trips/${encodeURIComponent(session.tripId)}/end`, {});
  }, [session.tripId, stopTracking]);

  return {
    phase, loaded, stepIndex, sender, requests, problem, fix, helpAskedAt, offlineSim,
    outboxDropped: engine.current?.outbox.dropped ?? 0,
    offline: { appShell: shellReady, media, persisted },
    start, next, back, askForHelp, finish,
    closeHelp: () => setHelpAskedAt(null),
    setOfflineSim: (on) => netsim.set(on),
    checkInNow: async () => {
      await engine.current?.tracker.checkInNow();
    },
  };
}
