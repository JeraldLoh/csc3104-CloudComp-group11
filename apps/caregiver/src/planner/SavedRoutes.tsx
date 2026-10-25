// The saved-routes list for one traveller. Give it to <RoutePlanner sections> (as the tab with
// id "saved") so an opened route is drawn on the planner's map, or use it anywhere else.
import { useEffect, useState } from "react";
import { RouteEditor } from "./RouteEditor";
import type { SavedRoutesState } from "./useSavedRoutes";
import type { TripsState } from "./useTrips";
import "./planner.css";

interface Props {
  saved: SavedRoutesState;
  /** Optional: lets the editor say how many trips an edit would stop. */
  trips?: TripsState;
}

export function SavedRoutes({ saved, trips }: Props) {
  const { routes, loading, status, opened, approvingId } = saved;
  const [editing, setEditing] = useState(false);
  /** The route whose "Delete?" question is showing. */
  const [asking, setAsking] = useState<string | null>(null);
  // A different route, a reload or a saved edit all end editing.
  useEffect(() => setEditing(false), [opened]);

  const affectedTrips = opened && trips
    ? trips.trips.filter((t) => t.routeId === opened.routeId && t.status !== "completed").length
    : 0;

  // Trips made on a route: a delete stops the planned ones, and is refused while one is under way.
  const tripsOn = (routeId: string) => (trips?.trips ?? []).filter((t) => t.routeId === routeId && t.status !== "completed");

  return (
    <div className="rp__saved">
      <section className="rp__card" aria-label="Saved routes">
        <h2 className="rp__heading">Saved routes</h2>

        {!saved.traveller ? (
          <div className="rp__meta">Choose a traveller to see their routes.</div>
        ) : loading && routes.length === 0 ? (
          <div className="rp__meta">Loading...</div>
        ) : routes.length === 0 ? (
          <div className="rp__meta">Nothing saved for this traveller yet.</div>
        ) : (
          <ul className="rp__list">
            {routes.map((r) => {
              const live = tripsOn(r.routeId);
              const underWay = live.some((t) => t.status === "active");
              return (
                <li key={r.routeId} className="rp__route">
                  <div className="rp__item">
                    <button
                      type="button"
                      className="rp__open"
                      aria-current={opened?.routeId === r.routeId}
                      onClick={() => void saved.open(r.routeId)}
                    >
                      <span className="rp__name">{r.name}</span>
                      <span className="rp__meta">
                        <code>{r.routeId}</code>{`, version ${r.version}`}
                      </span>
                      <span className={"rp__badge" + (r.approved ? " rp__badge--ok" : "")}>{r.approved ? "Approved" : "Not approved"}</span>
                    </button>
                    <div className="rp__row-actions">
                      {!r.approved && (
                        <button
                          type="button"
                          className="rp__btn rp__btn--small"
                          disabled={approvingId === r.routeId}
                          aria-label={`Approve ${r.name}`}
                          onClick={() => void saved.approve(r.routeId)}
                        >
                          Approve
                        </button>
                      )}
                      <button
                        type="button"
                        className="rp__btn rp__btn--secondary rp__btn--small"
                        aria-label={`Delete ${r.name}`}
                        aria-expanded={asking === r.routeId}
                        onClick={() => setAsking(asking === r.routeId ? null : r.routeId)}
                      >
                        Delete
                      </button>
                    </div>
                  </div>
                  {asking === r.routeId && (
                    <div className="rp__confirm" role="group" aria-label={`Delete ${r.name}?`}>
                      <div>
                        {underWay
                          ? "A trip on this route is under way. End it on the Trips tab before deleting the route."
                          : `Delete ${r.name} (${r.routeId})? This can't be undone.`}
                        {!underWay && live.length > 0 &&
                          ` ${live.length} planned trip${live.length > 1 ? "s" : ""} on this route will stop working.`}
                      </div>
                      <div className="rp__row">
                        <button
                          type="button"
                          className="rp__btn rp__btn--danger"
                          disabled={underWay || saved.deletingId === r.routeId}
                          aria-label={`Yes, delete ${r.name}`}
                          onClick={async () => { if (await saved.remove(r)) setAsking(null); }}
                        >
                          {saved.deletingId === r.routeId ? "Deleting..." : "Yes, delete"}
                        </button>
                        <button type="button" className="rp__btn rp__btn--secondary" onClick={() => setAsking(null)}>Keep it</button>
                      </div>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}

        {saved.status.text && (
          <div role="status" className={"rp__status" + (status.error ? " rp__status--error" : "")}>{saved.status.text}</div>
        )}

        {saved.outOfDate && opened && (
          <button type="button" className="rp__btn rp__btn--secondary" onClick={() => void saved.open(opened.routeId)}>
            Reload this route
          </button>
        )}
      </section>

      {opened && (
        <section className="rp__card rp__opened" aria-label="Opened route">
          <div className="rp__opened-head">
            <div>
              <h2 className="rp__heading">{opened.name}</h2>
              <div className="rp__meta">
                <code>{opened.routeId}</code>{`, version ${opened.version}, `}
                <span className={opened.approved ? "rp__badge--ok" : "rp__badge--warn"}>{opened.approved ? "approved" : "not approved"}</span>
              </div>
            </div>
            <div className="rp__opened-actions">
              {!editing && (
                <button type="button" className="rp__btn rp__btn--secondary rp__btn--small" onClick={() => setEditing(true)}>Edit</button>
              )}
              <button type="button" className="rp__btn rp__btn--secondary rp__btn--small" onClick={saved.close}>Close</button>
            </div>
          </div>
          {editing ? (
            <RouteEditor
              route={opened}
              affectedTrips={affectedTrips}
              saving={saved.saving}
              onSave={(changes) => void saved.update(changes)}
              onCancel={() => setEditing(false)}
            />
          ) : (
            <>
              <ol className="rp__steps">
                {opened.steps.map((step) => (
                  <li key={step.stepIndex} className={`rp__step rp__step--${step.mode}`}>
                    <div>{step.instruction}</div>
                    {step.easyRead && <div className="rp__easy">{step.easyRead}</div>}
                  </li>
                ))}
              </ol>
              <div className="rp__meta">The line under each step is the wording the traveller reads.</div>
            </>
          )}
        </section>
      )}
    </div>
  );
}
