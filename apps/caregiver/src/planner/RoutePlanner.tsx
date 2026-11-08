// Route planner for caregivers: find two places, plan, look at the options on the map, and
// save the one you want as a route for a traveller. It takes everything from outside:
//
//   <RoutePlanner api={api} travellerId="U-042" caregiverId="G-003" onSaved={(route) => ...} />
//
// The panel is split into tabs: "Plan" is always there, and a host adds its own after it with
// `sections` (e.g. saved routes, trips). It fills the height and width of its parent, and
// keeps its own styles under `.rp`.
import { useEffect, useId, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";
import type { DisruptionsApi, PlannerApi } from "../journey/api";
import type { RouteStep, SavedRoute } from "../journey/types";
import { MapView } from "./MapView";
import { OptionDetails, OptionList } from "./OptionDetails";
import { PreviewSlot } from "./PhonePreview";
import { PlaceField } from "./PlaceField";
import { useDisruptions, withLatest } from "./useDisruptions";
import { usePlanner, type PlannerOptions, type Point } from "./usePlanner";
import "./planner.css";

/** Which plan option a check belongs to: its steps' modes, lines and stops (a new plan gives a new key). */
const stepsKey = (steps: RouteStep[]) => JSON.stringify(steps.map((s) => [s.mode, s.line, s.from.code, s.to.code]));
const NO_CHECKS: DisruptionsApi = { checkDisruptions: () => Promise.reject(new Error("not supported")) };

/** A tab after "Plan". Give the saved routes the id "saved" and saving a route switches to it. */
export interface PlannerSection {
  id: string;
  label: string;
  /** Shown next to the label, e.g. how many routes there are. */
  count?: number;
  content: ReactNode;
  /** Drawn in place of the planner's map while this tab is open (e.g. <TripMap>), so clicks there don't change From and To. */
  stage?: ReactNode;
}

export interface RoutePlannerProps {
  /** With `checkDisruptions` (JourneyApi has it), the selected option's disruption warnings are re-checked every minute. */
  api: PlannerApi & Partial<DisruptionsApi>;
  travellerId: string;
  caregiverId: string;
  initial?: PlannerOptions["initial"];
  /** Called after a route is saved (not approved yet). */
  onSaved?: (route: SavedRoute) => void;
  /** A saved route to show on the map. Each new object is shown; planning or picking an option shows the plan again. */
  viewing?: SavedRoute | null;
  /** Plan a new route for a trip under way (each new object starts it, on the Plan tab): saved, it is linked to the trip. */
  reroute?: { tripId: string; from: Point; to: Point } | null;
  /** Open this tab (each new object), e.g. Saved routes after opening a route there. */
  goTo?: { tab: string } | null;
  /** Extra tabs after "Plan", e.g. <SavedRoutes> and <Trips>. */
  sections?: PlannerSection[];
  className?: string;
}

const PLAN = "plan";
/** The side panel's width, dragged by its edge: at least MIN_PANEL px, at most MAX_PANEL of the planner, kept per browser. */
const MIN_PANEL = 320;
const MAX_PANEL = 0.7;
const PANEL_KEY = "careroute.panelWidth";

function savedWidth(): number | null {
  try {
    const w = Number(localStorage.getItem(PANEL_KEY));
    return w >= MIN_PANEL ? w : null;
  } catch {
    return null;
  }
}

export function RoutePlanner({ api, travellerId, caregiverId, initial, onSaved, viewing = null, reroute = null, goTo = null, sections = [], className }: RoutePlannerProps) {
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
  useEffect(() => {
    if (!reroute) return;
    setTab(PLAN);
    p.reroute(reroute.tripId, reroute.from, reroute.to);
  }, [reroute]);
  useEffect(() => { if (goTo) setTab(goTo.tab); }, [goTo]);
  const root = useRef<HTMLDivElement>(null);
  const [panelWidth, setPanelWidth] = useState<number | null>(savedWidth);
  function resizePanel(width: number | null) {
    const room = root.current?.clientWidth ?? 1200;
    const next = width === null ? null : Math.round(Math.max(MIN_PANEL, Math.min(room * MAX_PANEL, width)));
    setPanelWidth(next);
    try {
      if (next === null) localStorage.removeItem(PANEL_KEY);
      else localStorage.setItem(PANEL_KEY, String(next));
    } catch { /* no storage (private window): the width lasts until the page is closed */ }
  }
  function dragPanel(e: PointerEvent<HTMLDivElement>) {
    if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
    resizePanel(e.clientX - (root.current?.getBoundingClientRect().left ?? 0));
  }
  function onHandleKey(e: KeyboardEvent<HTMLDivElement>) {
    const step = e.key === "ArrowRight" ? 32 : e.key === "ArrowLeft" ? -32 : 0;
    if (!step) return;
    e.preventDefault();
    resizePanel((root.current?.querySelector<HTMLElement>(".rp__panel")?.offsetWidth ?? panelWidth ?? 400) + step);
  }

  // Over the map: where the route editor shows its phone preview.
  const [previewSlot, setPreviewSlot] = useState<HTMLDivElement | null>(null);

  // A plan's disruption warnings are from the moment it was made: keep the selected option re-checked.
  const selected = p.activeOption;
  const planKey = selected ? stepsKey(selected.steps) : "";
  const checks = useDisruptions(api.checkDisruptions ? (api as DisruptionsApi) : NO_CHECKS,
    selected ? { [planKey]: selected.steps } : {}, { enabled: Boolean(api.checkDisruptions) });
  const shownOption = selected && { ...selected, warnings: withLatest(selected.warnings, checks[planKey]) };

  // Whatever was asked for last is on the map. (Order matters: on first render a route given
  // up front wins over the empty plan.)
  const [showSaved, setShowSaved] = useState(false);
  useEffect(() => setShowSaved(false), [p.options, p.activeIndex]);
  useEffect(() => setShowSaved(viewing !== null), [viewing]);

  const tabs = [{ id: PLAN, label: "Plan", count: undefined }, ...sections];
  const current = tabs.some((t) => t.id === tab) ? tab : PLAN;
  const stage = sections.find((s) => s.id === current)?.stage;

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
    <PreviewSlot.Provider value={previewSlot}>
    <div ref={root} className={"rp" + (className ? " " + className : "")}>
      <div className="rp__panel" style={panelWidth ? { width: panelWidth, maxWidth: `${MAX_PANEL * 100}%` } : undefined}>
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
          {p.forTrip && (
            <div className="rp__notice" role="status">
              <strong>{`New route for trip ${p.forTrip}`}</strong>
              {", from where the traveller is. Pick an option and save it: it is named \"Re-route\". Then check its wording "
                + "and photos on Saved routes, approve it, and send it from the Trips tab."}
              <button type="button" className="rp__btn rp__btn--secondary rp__btn--small rp__spaced" onClick={p.unlinkTrip}>
                Plan an ordinary route instead
              </button>
            </div>
          )}
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

          {shownOption && (
            <section className="rp__card" aria-label="Steps">
              <h2 className="rp__heading">{`Steps for option ${shownOption.option}`}</h2>
              <OptionDetails option={shownOption} />
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

      <div
        className="rp__resize" role="separator" aria-orientation="vertical" tabIndex={0}
        aria-label="Side panel width: drag, or use the left and right arrow keys (double-click to reset)"
        aria-valuenow={panelWidth ?? undefined}
        onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); e.preventDefault(); }}
        onPointerMove={dragPanel}
        onPointerUp={(e) => e.currentTarget.releasePointerCapture(e.pointerId)}
        onDoubleClick={() => resizePanel(null)}
        onKeyDown={onHandleKey}
      />
      <div className="rp__stage">
        {stage ?? (
          <MapView
            from={p.points.from} to={p.points.to} focus={p.focus}
            onMapClick={current === PLAN ? p.clickMap : undefined}   /* only planning sets From and To */
            option={showSaved ? null : shownOption} saved={showSaved ? viewing : null}
            affected={showSaved ? undefined : selected?.affected}
          />
        )}
        <div className="rp__preview-slot" ref={setPreviewSlot} />
      </div>
    </div>
    </PreviewSlot.Provider>
  );
}
