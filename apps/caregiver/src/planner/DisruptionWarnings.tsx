// Disruption warnings (from a plan or useDisruptions): laid over the step they affect, or listed when there is
// no step to put them on (the Trips tab, or a warning about the whole route).
import type { ReactNode } from "react";
import type { DisruptionCheck, PlanWarning } from "../journey/types";

/** Warning types that belong to one step and are shown over it. */
const ON_THE_STEP = new Set(["train_disruption", "bus_notice"]);

const time = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
export const warningClass = (w: PlanWarning) => "rp__warning" + (w.severity === "high" ? " rp__warning--high" : "");

/** The disruption warnings for each step (by stepIndex), and everything else. */
export function splitDisruptions(warnings: PlanWarning[]): { byStep: Map<number, PlanWarning[]>; rest: PlanWarning[] } {
  const byStep = new Map<number, PlanWarning[]>();
  const rest: PlanWarning[] = [];
  for (const w of warnings) {
    if (ON_THE_STEP.has(w.type) && w.stepIndex !== null) byStep.set(w.stepIndex, [...(byStep.get(w.stepIndex) ?? []), w]);
    else rest.push(w);
  }
  return { byStep, rest };
}

/** One step of a list (`<li>`). With disruption warnings, they are laid over it and the step's own text fades. */
export function DisruptedStep({ className, warnings, children }: { className: string; warnings?: PlanWarning[]; children: ReactNode }) {
  if (!warnings?.length) return <li className={className}>{children}</li>;
  return (
    <li className={`${className} rp__step--disrupted`}>
      <div className="rp__step-body">{children}</div>
      <div className="rp__step-overlay" role="note" aria-label="Disruption on this step">
        {warnings.map((w, i) => <div key={i}>{w.message}</div>)}
      </div>
    </li>
  );
}

/**
 * A list of warnings (those with a step say which), then when they were checked. `onSteps: false` leaves out
 * the ones already shown over their step. Nothing until the first check is back, or when there's nothing to say.
 */
export function DisruptionWarnings({ check, onSteps = true }: { check?: DisruptionCheck; onSteps?: boolean }) {
  if (!check || check.warnings.length === 0) return null;
  const listed = onSteps ? check.warnings : splitDisruptions(check.warnings).rest;
  return (
    <div aria-label="Disruptions">
      {listed.map((w, i) => (   // (with onSteps false, only the time is left when every warning is on a step)
        <div key={i} className={warningClass(w)}>
          {w.stepIndex !== null && <strong>{`Step ${w.stepIndex + 1}: `}</strong>}
          {w.message}
        </div>
      ))}
      <div className="rp__meta">{`Disruptions checked at ${time(check.checkedAt)}; checked again every minute.`}</div>
    </div>
  );
}
