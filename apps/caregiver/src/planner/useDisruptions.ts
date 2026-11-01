// Keeps planned and saved steps checked against LTA's current alerts. A plan only knows about the
// disruptions at the moment it was made; this asks the service again (POST /api/routes/check-disruptions)
// when the steps change and then every minute, so an MRT fault that starts later still shows up.
// Like the other hooks it has no screen in it.
import { useEffect, useRef, useState } from "react";
import type { DisruptionsApi } from "../journey/api";
import type { DisruptionCheck, PlanWarning, RouteStep } from "../journey/types";

// How often to re-check the selected option's steps for new disruption warnings. The service caches the result for a minute, so no need to check more often.
export const RECHECK_MS = 60_000;

// The disruption types that are checked for new warnings. Others (e.g. "bus_delay") are only known at the moment of planning, so they are not re-checked.
const DISRUPTION_TYPES = new Set(["train_disruption", "bus_notice", "live_data_unavailable"]);

// Add the latest check's warnings to the plan's original ones, replacing any of the same type (e.g. a new train disruption replaces the old one).
export function withLatest(warnings: PlanWarning[], check: DisruptionCheck | undefined): PlanWarning[] {
  if (!check) return warnings;
  return [...warnings.filter((w) => !DISRUPTION_TYPES.has(w.type)), ...check.warnings];
}

// Keep the latest disruption check for each set of steps, re-checking every minute. The service caches the result for a minute, so no need to check more often.
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
