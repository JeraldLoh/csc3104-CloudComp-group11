// Keeps planned and saved steps checked against LTA's current alerts. A plan only knows about the
// disruptions at the moment it was made; this asks the service again (POST /api/routes/check-disruptions)
// when the steps change and then every minute, so an MRT fault that starts later still shows up.
// Like the other hooks it has no screen in it.
import { useEffect, useRef, useState } from "react";
import type { DisruptionsApi } from "../journey/api";
import type { DisruptionCheck, PlanWarning, RouteStep } from "../journey/types";

/** LTA's train alerts are cached for about a minute by the service, so checking more often shows nothing new. */
export const RECHECK_MS = 60_000;

/** The warning types a re-check replaces (the others, like long walks, don't change after planning). */
const DISRUPTION_TYPES = new Set(["train_disruption", "bus_notice", "live_data_unavailable"]);

/** A plan's own warnings, with its disruption warnings swapped for the latest check (when there is one). */
export function withLatest(warnings: PlanWarning[], check: DisruptionCheck | undefined): PlanWarning[] {
  if (!check) return warnings;
  return [...warnings.filter((w) => !DISRUPTION_TYPES.has(w.type)), ...check.warnings];
}

/**
 * The latest check for each set of steps, by the same key (e.g. a route id). A set that fails to check keeps its
 * last answer, so a short outage doesn't hide a known disruption. `enabled: false` stops checking.
 */
export function useDisruptions(
  api: DisruptionsApi,
  sets: Record<string, RouteStep[]>,
  { enabled = true, intervalMs = RECHECK_MS }: { enabled?: boolean; intervalMs?: number } = {},
): Record<string, DisruptionCheck> {
  const latestApi = useRef(api);
  latestApi.current = api;
  const [checks, setChecks] = useState<Record<string, DisruptionCheck>>({});
  // The sets compared by content, so a host that rebuilds the object on every render doesn't re-check every render.
  const signature = JSON.stringify(sets);

  useEffect(() => {
    const current: Record<string, RouteStep[]> = JSON.parse(signature);
    const keys = Object.keys(current);
    setChecks((old) => Object.fromEntries(Object.entries(old).filter(([k]) => k in current)));
    if (!enabled || keys.length === 0) return;
    let stopped = false;

    async function checkAll() {
      await Promise.all(keys.map(async (key) => {
        try {
          const check = await latestApi.current.checkDisruptions(current[key]);
          if (!stopped) setChecks((old) => ({ ...old, [key]: check }));
        } catch { /* keep the last answer; the next round tries again */ }
      }));
    }

    void checkAll();
    const timer = setInterval(() => void checkAll(), intervalMs);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [signature, enabled, intervalMs]);

  return checks;
}
