// Route planner for caregivers: find two places, plan, look at the options on the map, and
// save the one you want as a route for a traveller. It takes everything from outside:
//
//   <RoutePlanner api={api} travellerId="U-042" caregiverId="G-003" onSaved={(route) => ...} />
//
// The panel is split into tabs: "Plan" is always there, and a host adds its own after it with
// `sections` (e.g. saved routes, trips). It fills the height and width of its parent, and
// keeps its own styles under `.rp`.
import { useEffect, useId, useState, type KeyboardEvent, type ReactNode } from "react";
import type { PlannerApi } from "../journey/api";
import type { SavedRoute } from "../journey/types";
import { MapView } from "./MapView";
import { OptionDetails, OptionList } from "./OptionDetails";
import { PlaceField } from "./PlaceField";
import { usePlanner, type PlannerOptions } from "./usePlanner";
import "./planner.css";

/** A tab after "Plan". Give the saved routes the id "saved" and saving a route switches to it. */
export interface PlannerSection {
  id: string;
  label: string;
  /** Shown next to the label, e.g. how many routes there are. */
  count?: number;
  content: ReactNode;
}

export interface RoutePlannerProps {
  api: PlannerApi;
  travellerId: string;
  caregiverId: string;
  initial?: PlannerOptions["initial"];
  /** Called after a route is saved (not approved yet). */
  onSaved?: (route: SavedRoute) => void;
  /** A saved route to show on the map. Each new object is shown; planning or picking an option shows the plan again. */
  viewing?: SavedRoute | null;
  /** Extra tabs after "Plan", e.g. <SavedRoutes> and <Trips>. */
  sections?: PlannerSection[];
  className?: string;
}

const PLAN = "plan";

export function RoutePlanner({ api, travellerId, caregiverId, initial, onSaved, viewing = null, sections = [], className }: RoutePlannerProps) {
  const [tab, setTab] = useState(PLAN);
  const hasSavedTab = sections.some((s) => s.id === "saved");
  const p = usePlanner(api, {
    travellerId, caregiverId, initial,
    onSaved: (route) => {
      if (hasSavedTab) setTab("saved");        // show the new route where it now lives
      onSaved?.(route);
    },
  });
  const ids = { sort: useId(), mode: useId(), name: useId(), tabs: useId() };

  // Whatever was asked for last is on the map. (Order matters: on first render a route given
  // up front wins over the empty plan.)
  const [showSaved, setShowSaved] = useState(false);
  useEffect(() => setShowSaved(false), [p.options, p.activeIndex]);
  useEffect(() => setShowSaved(viewing !== null), [viewing]);

  const tabs = [{ id: PLAN, label: "Plan", count: undefined }, ...sections];
  const current = tabs.some((t) => t.id === tab) ? tab : PLAN;

  // Left and right arrows move between tabs, as in a native tab list.
  function onTabKey(e: KeyboardEvent<HTMLDivElement>) {
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    e.preventDefault();
    const next = tabs[(tabs.findIndex((t) => t.id === current) + step + tabs.length) % tabs.length];
    setTab(next.id);
    document.getElementById(`${ids.tabs}-tab-${next.id}`)?.focus();
  }

  return (
    <div className={"rp" + (className ? " " + className : "")}>
      <div className="rp__panel">
        <div className="rp__tabs" role="tablist" aria-label="Caregiver tools" onKeyDown={onTabKey}>
          {tabs.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              id={`${ids.tabs}-tab-${t.id}`}
              aria-selected={t.id === current}
              aria-controls={`${ids.tabs}-panel-${t.id}`}
              tabIndex={t.id === current ? 0 : -1}
              className={"rp__tab" + (t.id === current ? " rp__tab--active" : "")}
              onClick={() => setTab(t.id)}
            >
              {t.label}
              {t.count !== undefined && t.count > 0 && <span className="rp__count">{t.count}</span>}
            </button>
          ))}
        </div>

        <div
          className="rp__body" role="tabpanel" hidden={current !== PLAN}
          id={`${ids.tabs}-panel-${PLAN}`} aria-labelledby={`${ids.tabs}-tab-${PLAN}`}
        >
          <section className="rp__card" aria-label="Places">
            <h2 className="rp__heading">Trip</h2>
            <p className="rp__hint">Search for a place, or click the map: first click = from, second = to.</p>

            <PlaceField
              label="From" value={p.queries.from} point={p.points.from} candidates={p.candidates.from}
              onChange={(t) => p.setQuery("from", t)} onSearch={() => void p.lookUp("from")}
              onPick={(place) => p.choosePlace("from", place)}
            />
            <PlaceField
              label="To" value={p.queries.to} point={p.points.to} candidates={p.candidates.to}
              onChange={(t) => p.setQuery("to", t)} onSearch={() => void p.lookUp("to")}
              onPick={(place) => p.choosePlace("to", place)}
            />

            <div className="rp__pair">
              <div className="rp__field">
                <label htmlFor={ids.sort}>Sort by</label>
                <select id={ids.sort} value={p.sortBy} onChange={(e) => p.setSortBy(e.target.value as typeof p.sortBy)}>
                  <option value="walking">Least walking</option>
                  <option value="crowding">Least crowded</option>
                  <option value="time">Fastest (OneMap's order)</option>
                </select>
              </div>
              <div className="rp__field">
                <label htmlFor={ids.mode}>Travel by</label>
                <select id={ids.mode} value={p.mode} onChange={(e) => p.setMode(e.target.value as typeof p.mode)}>
                  <option value="any">Bus or train</option>
                  <option value="bus">Bus only</option>
                  <option value="train">Train only</option>
                </select>
              </div>
            </div>

            <div className="rp__row">
              <button type="button" className="rp__btn" disabled={p.busy} onClick={() => void p.plan()}>Plan route</button>
              <button type="button" className="rp__btn rp__btn--secondary" onClick={p.clear}>Clear</button>
            </div>

            <div role="status" className={"rp__status" + (p.status.error ? " rp__status--error" : "")}>{p.status.text}</div>
          </section>

          {p.options.length > 0 && (
            <section className="rp__card" aria-label="Options">
              <h2 className="rp__heading">Options</h2>
              <OptionList options={p.options} activeIndex={p.activeIndex} onSelect={p.selectOption} />
            </section>
          )}

          {p.activeOption && (
            <section className="rp__card" aria-label="Steps">
              <h2 className="rp__heading">{`Steps for option ${p.activeOption.option}`}</h2>
              <OptionDetails option={p.activeOption} />
            </section>
          )}

          {p.planned && p.activeOption && (
            <section className="rp__card" aria-label="Save">
              <h2 className="rp__heading">Save this option</h2>
              <label htmlFor={ids.name}>Save the selected option as</label>
              <input id={ids.name} value={p.routeName} onChange={(e) => p.setRouteName(e.target.value)} />
              <button type="button" className="rp__btn rp__btn--spaced" disabled={p.saving} onClick={() => void p.save()}>Save route</button>
            </section>
          )}
        </div>

        {sections.map((s) => (
          <div
            key={s.id}
            className="rp__body" role="tabpanel" hidden={current !== s.id}
            id={`${ids.tabs}-panel-${s.id}`} aria-labelledby={`${ids.tabs}-tab-${s.id}`}
          >
            {s.content}
          </div>
        ))}
      </div>

      <MapView
        from={p.points.from} to={p.points.to} focus={p.focus} onMapClick={p.clickMap}
        option={showSaved ? null : p.activeOption} saved={showSaved ? viewing : null}
      />
    </div>
  );
}
