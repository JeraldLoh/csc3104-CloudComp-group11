// Changing a trip under way, on the Trips tab: the free bridging bus proposed during a train breakdown (sent to the
// traveller by itself when its countdown ends, unless the caregiver sends it sooner or chooses not to), what was
// sent, and a whole new route from where the traveller is: planned on the Plan tab, saved (named "Re-route (T-…)")
// and checked on Saved routes like any route, then sent from here.
import { useEffect, useState } from "react";
import type { BridgePreview, RouteStep, SavedRoute, Trip, TripChange as Change } from "../journey/types";
import { ConfirmButton } from "./ConfirmButton";
import { EasyReadPoints } from "./EasyReadPoints";
import { message } from "./status";
import { StepPhoto } from "./StepPhoto";
import type { TripsState } from "./useTrips";

const at = (iso: string | null) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "");

/** "2:41" until `iso`, "0:00" once it has passed; ticks every second. */
function useCountdown(iso: string | null): string {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!iso) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [iso]);
  const left = Math.max(0, Math.round(((iso ? Date.parse(iso) : now) - now) / 1000));
  return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
}

const busName = (c: Change) => (c.bridgeKind === "shuttle" ? "free bridging bus" : `free bus ${c.busNo ?? ""}`.trim());

/** Steps as the traveller will get them: the wording and the photo. */
export function Steps({ steps }: { steps: RouteStep[] }) {
  return (
    <ol className="rp__steps">
      {steps.map((s, i) => (
        <li key={i} className={`rp__step rp__step--${s.mode}`}>
          <div>{s.instruction}</div>
          <EasyReadPoints text={s.easyRead} />
          <StepPhoto url={s.photoUrl} alt={`Step ${i + 1}: where it ends`} fallback={null} />
        </li>
      ))}
    </ol>
  );
}

/** "Use the free bridging bus": first the steps it would give the trip, then the choice. */
function UseBridge({ trip, trips, started }: { trip: Trip; trips: TripsState; started: boolean }) {
  const [preview, setPreview] = useState<BridgePreview | null>(null);
  const [looking, setLooking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busy = trips.busyId === trip.tripId;
  async function look() {
    setLooking(true);
    setError(null);
    try {
      setPreview(await trips.previewBridge(trip.tripId));
    } catch (e) {
      setError(message(e));
    } finally {
      setLooking(false);
    }
  }
  if (preview) {
    const bus = preview.kind === "shuttle" ? "the free bridging bus" : `free bus ${preview.busNo ?? ""}`.trim();
    return (
      <div className="rp__confirm rp__confirm--inline rp__bridge-preview" role="group" aria-label="The new steps">
        <strong>{`The new steps, with ${bus} from ${preview.leaveAt} to ${preview.target}:`}</strong>
        <Steps steps={preview.steps} />
        <div className="rp__meta">
          {started ? "They replace the train ride through the breakdown, and the traveller's app shows them straight away."
            : "They replace the train ride through the breakdown; the traveller's app shows them when the trip starts."}
        </div>
        <div className="rp__row">
          <button type="button" className="rp__btn rp__btn--small" disabled={busy}
            onClick={() => { setPreview(null); void trips.applyBridge(trip.tripId); }}>Yes, use it</button>
          <button type="button" className="rp__btn rp__btn--secondary rp__btn--small" onClick={() => setPreview(null)}>Cancel</button>
        </div>
      </div>
    );
  }
  return (
    <>
      <button type="button" className="rp__btn rp__btn--small" disabled={busy || looking} onClick={() => void look()}>
        {looking ? "Working out the steps…" : "Use the free bridging bus"}
      </button>
      {error && <div className="rp__warning rp__confirm--inline">{error}</div>}
    </>
  );
}

export interface TripChangeProps {
  trip: Trip;
  trips: TripsState;
  /** The routes saved for this trip ("Re-route (T-…)"), newest first. */
  reroutes: SavedRoute[];
  /** Plan a new route on the Plan tab, from where the traveller is. */
  onReroute?: (trip: Trip) => void;
  /** Open a route on the Saved routes tab, to check its wording and photos. */
  onOpenRoute?: (routeId: string) => void;
  /** Trains aren't running on part of the trip's route now (its disruption check). */
  trainsDown?: boolean;
}

export function TripChange({ trip, trips, reroutes, onReroute, onOpenRoute, trainsDown = false }: TripChangeProps) {
  const change = trip.override;
  const busy = trips.busyId === trip.tripId;
  const countdown = useCountdown(change?.status === "pending" ? change.sendAt : null);
  if (trip.status === "completed") return null;
  const started = trip.status === "active";
  const bridge = change?.kind === "bridge" ? change : null;
  const sendIt = (what: string) => (
    <>{`Send ${what} to the traveller now? It takes them from ${bridge?.leaveAt} to ${bridge?.target}, `
      + "and their app shows the new steps straight away."}</>
  );
  // Nothing changed yet, a breakdown on the route: the caregiver can switch to the free bus past it now.
  const canUseBridge = trainsDown && !(change && ["pending", "dismissed", "sent"].includes(change.status));

  return (
    <div className="rp__change">
      {change?.kind === "bridge" && change.status === "pending" && (
        <div className="rp__warning rp__warning--high">
          <strong>{`Trains aren't running on part of this trip.`}</strong>
          {` A ${busName(change)} can take the traveller from ${change.leaveAt} to ${change.target}. `}
          {`It will be sent to them automatically in ${countdown} (at ${at(change.sendAt)}).`}
          <Steps steps={change.steps} />
          <div className="rp__row">
            <ConfirmButton className="rp__btn rp__btn--small" disabled={busy} yes="Yes, send it"
              confirm={sendIt(`the ${busName(change)}`)} onConfirm={() => void trips.sendBridge(trip.tripId)}>
              Send now
            </ConfirmButton>
            <button type="button" className="rp__btn rp__btn--secondary rp__btn--small" disabled={busy}
              onClick={() => void trips.dismissBridge(trip.tripId)}>
              {"Don't send"}
            </button>
          </div>
        </div>
      )}
      {/* Not sent: folded away, and can still be sent while the breakdown lasts. */}
      {bridge?.status === "dismissed" && (
        <details className="rp__dismissed">
          <summary>{`Free ${bridge.bridgeKind === "shuttle" ? "bridging bus" : `bus ${bridge.busNo ?? ""}`} not sent`}</summary>
          <div className="rp__meta">{`It can still take the traveller from ${bridge.leaveAt} to ${bridge.target} while trains aren't running.`}</div>
          <Steps steps={bridge.steps} />
          <ConfirmButton className="rp__btn rp__btn--small rp__spaced" disabled={busy} yes="Yes, send it"
            confirm={sendIt(`the ${busName(bridge)}`)} onConfirm={() => void trips.sendBridge(trip.tripId)}>
            Send it now
          </ConfirmButton>
        </details>
      )}
      {change?.status === "sent" && (
        <div className="rp__warning">
          {change.kind === "bridge"
            ? `${started ? "The traveller is now taking" : "This trip now uses"} the ${busName(change)} from ${change.leaveAt} to ${change.target}`
            : `${started ? "The traveller is now following" : "This trip now follows"} ${reroutes.find((r) => r.routeId === change.fromRoute)?.name ?? "a new route"}`}
          {` (${change.sentBy === "auto" ? "sent automatically" : "you chose it"} at ${at(change.sentAt)}). `
            + (started ? "Their app shows the new steps." : "Their app will show it when the trip starts.")}
        </div>
      )}

      {reroutes.length > 0 && (
        <ul className="rp__list rp__reroutes" aria-label="New routes saved for this trip">
          {reroutes.map((r) => {
            const sent = change?.status === "sent" && change.fromRoute === r.routeId;
            return (
              <li key={r.routeId} className="rp__reroute">
                <div className="rp__name">{r.name}</div>
                <div className={"rp__badge" + (r.approved ? " rp__badge--ok" : "")}>
                  {sent ? "Sent to the traveller" : r.approved ? "Approved: ready to send" : "Not approved yet"}
                </div>
                <div className="rp__row">
                  <button type="button" className="rp__btn rp__btn--secondary rp__btn--small" onClick={() => onOpenRoute?.(r.routeId)}>
                    Edit on Saved routes
                  </button>
                  {!sent && (
                    <button type="button" className="rp__btn rp__btn--small" disabled={busy || !r.approved}
                      title={r.approved ? undefined : "Approve it on Saved routes first"}
                      onClick={() => void trips.sendRoute(trip.tripId, r)}>
                      {started ? "Send to the traveller" : "Use for this trip"}
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
      {canUseBridge && !started && (
        <div className="rp__warning rp__warning--high">
          <strong>Trains aren't running on part of this route.</strong>
          {" Switch the trip to LTA's free bridging bus past the breakdown, or plan a new route."}
        </div>
      )}
      <div className="rp__row rp__change-actions">
        {canUseBridge && <UseBridge trip={trip} trips={trips} started={started} />}
        {onReroute && (
          <button type="button" className="rp__btn rp__btn--secondary rp__btn--small" disabled={busy}
            onClick={() => onReroute(trip)}>
            {started ? "Plan a new route from where the traveller is" : "Plan a new route"}
          </button>
        )}
      </div>
    </div>
  );
}
