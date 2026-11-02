// List of planned options, and the details of the selected one, with live info and notices.
import type { PlanOption } from "../journey/types";
import { DisruptedStep, ON_THE_STEP, splitDisruptions, warningClass } from "./DisruptionWarnings";
import { liveText, minutes, optionLabel } from "./format";

interface ListProps {
  options: PlanOption[];
  activeIndex: number;
  onSelect: (index: number) => void;
}

/** Rides where trains aren't running, or has an LTA disruption notice on one of its steps. */
const disrupted = (o: PlanOption) => Boolean(o.affected?.length) || o.warnings.some((w) => ON_THE_STEP.has(w.type));

export function OptionList({ options, activeIndex, onSelect }: ListProps) {
  return (
    <div className="rp__options">
      {options.map((o, i) => (
        <button
          key={o.option}
          type="button"
          className={"rp__option" + (i === activeIndex ? " rp__option--active" : "") + (disrupted(o) ? " rp__option--disrupted" : "")}
          aria-pressed={i === activeIndex}
          onClick={() => onSelect(i)}
        >
          {disrupted(o) && <span className="rp__option-flag">{o.affected?.length ? "Trains not running" : "Disruption"}</span>}
          {optionLabel(o)}
        </button>
      ))}
    </div>
  );
}

export function OptionDetails({ option }: { option: PlanOption }) {
  const liveByStep = new Map(option.live.map((l) => [l.stepIndex, l]));
  // Disruptions sit over the step they affect; the other notices (long walks, crowding...) stay below.
  const { byStep, rest } = splitDisruptions(option.warnings);
  return (
    <>
      <ol className="rp__steps">
        {option.steps.map((step) => {
          const live = liveByStep.get(step.stepIndex);
          return (
            <DisruptedStep key={step.stepIndex} className={`rp__step rp__step--${step.mode}`} warnings={byStep.get(step.stepIndex)}>
              <div>{step.instruction}</div>
              <div className="rp__meta">{minutes(step.expectedDurationS)} min</div>
              {live && <div className="rp__live">{liveText(live)}</div>}
            </DisruptedStep>
          );
        })}
      </ol>
      {rest.map((w, i) => <div key={i} className={warningClass(w)}>{w.message}</div>)}
      {option.affected?.map((a, i) => {
        const bridge = option.bridges?.find((b) => b.stepIndex === a.stepIndex);
        return (
          <div key={i} className="rp__bridge-note" role="note">
            <strong>{a.message}.</strong>
            {bridge ? (
              <>
                {` While the breakdown lasts, the traveller would take ${bridge.kind === "shuttle" ? "the free bridging bus" : `free bus ${bridge.busNo}`}:`}
                <ol className="rp__steps">
                  {bridge.steps.map((s, j) => <li key={j} className={`rp__step rp__step--${s.mode}`}>{s.instruction}</li>)}
                </ol>
                <span className="rp__meta">Not saved with the route: a trip under way gets it only if trains are still not running.</span>
              </>
            ) : " There is no free bus past it, so another option may be better."}
          </div>
        );
      })}
    </>
  );
}
