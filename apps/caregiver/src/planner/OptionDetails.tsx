// List of planned options, and the details of the selected one, with live info and notices.
import type { PlanOption } from "../journey/types";
import { DisruptedStep, splitDisruptions, warningClass } from "./DisruptionWarnings";
import { liveText, minutes, optionLabel } from "./format";

interface ListProps {
  options: PlanOption[];
  activeIndex: number;
  onSelect: (index: number) => void;
}

export function OptionList({ options, activeIndex, onSelect }: ListProps) {
  return (
    <div className="rp__options">
      {options.map((o, i) => (
        <button
          key={o.option}
          type="button"
          className={"rp__option" + (i === activeIndex ? " rp__option--active" : "")}
          aria-pressed={i === activeIndex}
          onClick={() => onSelect(i)}
        >
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
    </>
  );
}
