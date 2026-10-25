import type { PlanOption } from "../journey/types";
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

/** The steps of the selected option, with what is known live about each, and the notices. */
export function OptionDetails({ option }: { option: PlanOption }) {
  const liveByStep = new Map(option.live.map((l) => [l.stepIndex, l]));
  return (
    <>
      <ol className="rp__steps">
        {option.steps.map((step) => {
          const live = liveByStep.get(step.stepIndex);
          return (
            <li key={step.stepIndex} className={`rp__step rp__step--${step.mode}`}>
              <div>{step.instruction}</div>
              <div className="rp__meta">{minutes(step.expectedDurationS)} min</div>
              {live && <div className="rp__live">{liveText(live)}</div>}
            </li>
          );
        })}
      </ol>
      {option.warnings.map((w, i) => (
        <div key={i} className={"rp__warning" + (w.severity === "high" ? " rp__warning--high" : "")}>{w.message}</div>
      ))}
    </>
  );
}
