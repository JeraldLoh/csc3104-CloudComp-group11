"""Changing a trip under way, without changing its route: LTA's free bridging bus past a train breakdown, or a whole
new route from where the traveller is. Also where the traveller is now (`trip_location`) and the steps they follow
(`trip_plan`), which both follow those changes.

    client.watch_bridges(now)                      proposes a bridging bus for trips riding through a breakdown
    client.send_bridge / use_bridge / dismiss_bridge / bridge_preview
    client.replan_options(trip_id)                 new routes from the traveller's newest check-in or current step
    client.apply_replan(...) / send_route(...)     send one: the steps from the current one on are replaced

A mixin of JourneyClient (journey_client.py): these methods use the client's stores, planner and photos as `self`.
How a bridge is worked out: bridge.py; where trains aren't running: planning.py (`self.planner`).
"""
import json
import logging
from datetime import datetime, timedelta

from . import bridge
from .auth import Caller, Forbidden
from .easy_read import route_wording
from .geo import path_between
from .lta import SST, LtaError
from .timefmt import iso_utc
from .trip_store import TripConflict

log = logging.getLogger("journey")


class TripChanges:
    # ===== trip changes: a free bridging bus past a train breakdown, or a whole new route =====
    #
    # A trip keeps its route version (a disruption must never change the planned route); what changes is the
    # trip's own plan: `planSteps` (the steps the traveller now follows, in the pack and in Redis) and
    # `planVersion` (+1 per change, so the pack is a new version and the phone shows "Your trip has changed").
    # `override` is the latest change or proposal: {kind: "bridge"|"replan", status: "pending"|"sent"|"dismissed"|
    # "withdrawn", replacesFrom, replacesTo, steps, ...}. A bridging bus (bridge.py) is held for the caregiver for
    # BRIDGE_AUTO_SEND_S, then sent by itself (watch_bridges); a re-route is only ever the caregiver's choice. Each
    # step is also told to the caregiver through the disruptions queue (`notify`), which the Alert Service turns into
    # the trip's disruption incident.

    def _active_plan(self, trip: dict) -> tuple[dict, list[dict]]:
        route = self._route_for_trip(trip)
        return route, self._plan_for_trip(trip, route)

    def _current_step(self, trip_id: str) -> int:
        """Where the traveller is: `trip:{id}:context` stepIndex (the Check-in Service's), 0 before any check-in."""
        try:
            return int(self._redis().hget(f"trip:{trip_id}:context", "stepIndex") or 0)
        except (TypeError, ValueError):
            return 0

    def watch_bridges(self, now: datetime | None = None) -> dict:
        """One pass over the active trips (the API runs it every BRIDGE_WATCH_S; demo_tools `bridge` runs one now):
        propose a bridging bus where a trip rides into a broken stretch that has one, send the ones the caregiver
        left for BRIDGE_AUTO_SEND_S, and withdraw proposals whose breakdown has ended."""
        now = now or self._clock()
        summary = {"proposed": 0, "sent": 0, "withdrawn": 0}
        try:
            alerts = self.lta.train_alerts()
        except LtaError as e:
            log.warning("bridging buses: no train alerts (%s)", type(e).__name__)
            return summary
        broken = alerts["segments"] if alerts["status"] >= self._s.disruption_min_status else []
        for raw in sorted(self._redis().smembers("active-trips")):
            trip_id = raw.decode() if isinstance(raw, bytes) else raw
            trip = self._trip_store().get(trip_id)
            if trip is None or trip["status"] != "active":
                continue
            try:
                route, plan = self._active_plan(trip)
            except TripConflict:
                continue
            found = self._bridge_for_trip(trip_id, plan, broken)
            offer = trip.get("override") or {}
            pending = offer.get("kind") == "bridge" and offer.get("status") == "pending"
            # A dismissed one can still be sent while its breakdown lasts; both go when it ends.
            undecided = offer.get("kind") == "bridge" and offer.get("status") in ("pending", "dismissed")
            if undecided and (found is None or found[0] != offer.get("disruptionKey")):
                self._change_trip(trip, {**offer, "status": "withdrawn"})
                summary["withdrawn"] += 1
                continue
            if pending:
                if now >= datetime.fromisoformat(offer["sendAt"]):
                    self._send_override(trip, route, plan, offer, by="auto", now=now)
                    summary["sent"] += 1
                continue
            if found is None or (offer.get("kind") == "bridge" and offer.get("disruptionKey") == found[0]):
                continue                                     # nothing to offer, or this one was already decided
            proposal = self._bridge_proposal(found, plan, route, now)
            self._change_trip(trip, proposal)
            self._tell_caregiver(trip, proposal["replacesFrom"], proposal,
                                 f"A free {self._bridge_name(proposal)} can take the traveller from {proposal['leaveAt']} "
                                 f"to {proposal['target']}. It will be sent to them at {self._clock_time(proposal['sendAt'])} "
                                 "unless you send it sooner, choose not to, or plan a new route.")
            summary["proposed"] += 1
        return summary

    def _bridge_proposal(self, found: tuple, plan: list[dict], route: dict, now: datetime) -> dict:
        """The trip `override` offering _bridge_for_trip()'s bus, worded, to send at now + BRIDGE_AUTO_SEND_S."""
        key, index, segment, made = found
        return {
            "kind": "bridge", "status": "pending", "disruptionKey": key, "line": segment.get("line"),
            "bridgeKind": made["kind"], "busNo": next(s["line"] for s in made["steps"] if s.get("bridge")),
            "leaveAt": made["leaveAt"].get("name"), "target": made["target"].get("name"),
            "replacesFrom": index, "replacesTo": index,
            "steps": self.photos.add_street_photos({"steps": [{**s, "easyRead": t} for s, t in
                                                  zip(made["steps"], self._wording(plan, index, index, made["steps"], route))]})["steps"],
            "createdAt": iso_utc(now), "sendAt": iso_utc(now + timedelta(seconds=self._s.bridge_auto_send_s)),
            "sentAt": None, "sentBy": None,
        }

    def _bridge_for_trip(self, trip_id: str, plan: list[dict], segments: list[dict]):
        """(key, step index, segment, bridge.bridge_for result) for the first broken stretch the traveller's plan still
        rides through and that has a free bus, else None."""
        current = self._current_step(trip_id)
        for segment in segments:
            index = bridge.affected_step(plan, current, segment)
            if index is None:
                continue
            made = bridge.bridge_for(plan[index], segment, self.planner.free_bus, self._station_point)
            if made:
                return f"{segment.get('line')}:{','.join(segment['stations'])}", index, segment, made
        return None

    def _station_point(self, station: dict) -> tuple[float, float] | None:
        """Where a station is, for one the route only knows by name (the middle of its exits, from OneMap)."""
        exits = self.station_exits(station.get("name") or "")
        if not exits:
            return None
        return sum(e[0] for e in exits) / len(exits), sum(e[1] for e in exits) / len(exits)

    @staticmethod
    def _wording(plan: list[dict], start: int, end: int, new: list[dict], route: dict) -> list[str]:
        """easy_read wording for `new` as part of the plan (it knows the step before, and the last step)."""
        spliced = bridge.splice(plan, start, end, new)
        return route_wording(spliced, (route.get("destination") or {}).get("name"))[start:start + len(new)]

    def _change_trip(self, trip: dict, override: dict, plan_steps: list[dict] | None = None,
                     plan_paths: list | None = None) -> dict:
        changed = {**trip, "override": override}
        if plan_steps is not None:
            changed = {**changed, "planSteps": plan_steps, "planVersion": (trip.get("planVersion") or 1) + 1,
                       "planPaths": plan_paths}
        self._trip_store().replace(changed, trip["status"])       # unchanged since it was read
        return changed

    def _send_override(self, trip: dict, route: dict, plan: list[dict], offer: dict, by: str,
                       now: datetime | None = None, step_paths: list | None = None) -> dict:
        """Switch the traveller to `offer`'s steps: a new plan (and pack version), republished for the detectors.
        `step_paths`: the new steps' lines on the map (a re-route's, from OneMap); else they are cut from the lines of
        the steps they replace (a bridging bus runs alongside the train line it stands in for)."""
        now = now or self._clock()
        a, b = offer["replacesFrom"], offer["replacesTo"]
        new_plan = bridge.splice(plan, a, b, offer["steps"])
        old = self._plan_paths(trip, route, plan)
        if not (step_paths and len(step_paths) == len(offer["steps"])):
            replaced = [p for part in old[a:b + 1] for p in part]
            step_paths = [self._step_path(replaced, s) for s in offer["steps"]]
        sent = {**offer, "status": "sent", "sentAt": iso_utc(now), "sentBy": by}
        # The new steps' audio before the new plan is saved: the phone fetches the new pack as soon as it sees it
        self._speak(offer["steps"])
        changed = self._change_trip(trip, sent, new_plan, [*old[:a], *step_paths, *old[b + 1:]])
        if changed["status"] != "active":
            return changed                                   # not started: start_trip publishes the new plan
        self._publish_active(changed, route)
        what = (f"the free {self._bridge_name(offer)} from {offer['leaveAt']} to {offer['target']}"
                if offer["kind"] == "bridge" else "a new route to the destination")
        who = "automatically, as nobody chose in time" if by == "auto" else "by their caregiver"
        self._tell_caregiver(changed, offer["replacesFrom"], sent,
                             f"The traveller has been sent {what} ({who}). Their app shows the new steps.")
        return changed

    @staticmethod
    def _bridge_name(offer: dict) -> str:
        return "bridging bus" if offer.get("bridgeKind") == "shuttle" else f"bus {offer.get('busNo')}"

    def _tell_caregiver(self, trip: dict, step_index: int, offer: dict, message: str) -> None:
        """A message on the disruptions queue (contracts/examples/disruption.json), so the caregiver's alert for this
        trip says what is happening. Best-effort: the caregiver app's Trips tab shows it too."""
        if self._notify is None:
            return
        event = {
            "disruptionId": f"D-{offer.get('line') or 'TRIP'}-{trip['tripId']}-{offer['kind']}-{offer['status']}",
            "source": "journey-service", "tripId": trip["tripId"], "travellerId": trip["travellerId"],
            "affectedStepIndex": step_index, "mode": "train", "line": offer.get("line"),
            "affectedStations": [x for x in (offer.get("leaveAt"), offer.get("target")) if x],
            "message": message, "detectedAt": iso_utc(self._clock()),
        }
        try:
            self._notify(event)
        except Exception as e:  # noqa: BLE001
            log.warning("couldn't tell the caregiver about trip %s (%s)", trip["tripId"], type(e).__name__)

    def _owned_active_trip(self, trip_id: str, caller: Caller | None, planned_too: bool = False) -> tuple[dict, dict, list[dict]]:
        """The trip (under way, or with `planned_too` not started yet either), its route and plan, for its caregiver."""
        trip = self._stored_trip(trip_id, caller)
        if trip["status"] != "active" and not (planned_too and trip["status"] == "planned"):
            raise TripConflict("Only a trip that is under way can be changed." if not planned_too
                               else "This trip has already finished.")
        route, plan = self._active_plan(trip)
        if caller is not None and not self._owns(caller, route):
            raise Forbidden("Only the route's caregiver can change the trip.")
        return trip, route, plan

    def send_bridge(self, trip_id: str, caller: Caller | None = None) -> dict:
        """The caregiver sends the proposed bridging bus now, instead of waiting for it to go by itself; or one they
        chose not to send, while its breakdown lasts."""
        trip, route, plan = self._owned_active_trip(trip_id, caller, planned_too=True)
        offer = trip.get("override") or {}
        if offer.get("kind") != "bridge" or offer.get("status") not in ("pending", "dismissed"):
            raise TripConflict("There is no bridging bus waiting to be sent.")
        return self._public_trip(self._send_override(trip, route, plan, offer, by="caregiver"))

    def bridge_preview(self, trip_id: str, caller: Caller | None = None) -> dict:
        """What use_bridge() would change the trip to, without changing it: {kind, busNo, leaveAt, target, steps}, the
        steps worded and with their photos, for the caregiver to read before choosing it."""
        trip, route, plan = self._owned_active_trip(trip_id, caller, planned_too=True)
        found = self._bridge_for_trip(trip_id, plan, self.planner.broken())
        if found is None:
            raise TripConflict("There is no free bus past a breakdown on this trip's steps. Plan a new route instead.")
        offer = self._bridge_proposal(found, plan, route, self._clock())
        return {"kind": offer["bridgeKind"], "busNo": offer["busNo"], "leaveAt": offer["leaveAt"], "target": offer["target"],
                "steps": [self.photos.step_with_urls(s) for s in offer["steps"]]}

    def use_bridge(self, trip_id: str, caller: Caller | None = None) -> dict:
        """The caregiver switches the trip to the free bus past a breakdown now (e.g. before it starts), instead of
        waiting for the watcher to propose it."""
        trip, route, plan = self._owned_active_trip(trip_id, caller, planned_too=True)
        found = self._bridge_for_trip(trip_id, plan, self.planner.broken())
        if found is None:
            raise TripConflict("There is no free bus past a breakdown on this trip's steps. Plan a new route instead.")
        offer = self._bridge_proposal(found, plan, route, self._clock())
        return self._public_trip(self._send_override(trip, route, plan, offer, by="caregiver"))

    def dismiss_bridge(self, trip_id: str, caller: Caller | None = None) -> dict:
        """The caregiver chooses not to send it (it won't be sent by itself either; they can still send it)."""
        trip, _, _ = self._owned_active_trip(trip_id, caller, planned_too=True)
        offer = trip.get("override") or {}
        if offer.get("kind") != "bridge" or offer.get("status") != "pending":
            raise TripConflict("There is no bridging bus waiting to be sent.")
        dismissed = {**offer, "status": "dismissed"}
        changed = self._change_trip(trip, dismissed)
        self._tell_caregiver(changed, offer["replacesFrom"], dismissed,
                             "The bridging bus will not be sent: the traveller keeps their planned steps.")
        return self._public_trip(changed)

    RECENT_CHECKIN_S = 300               # a check-in this recent is where the traveller is now

    def _checkins(self, trip_id: str) -> list[dict]:
        """The traveller's last check-ins (`trip:{id}:recent`, the Check-in Service's), newest first, as
        {lat, lng, at (datetime), timestamp}; any that can't be read are left out."""
        found = []
        for raw in self._redis().lrange(f"trip:{trip_id}:recent", 0, -1):
            try:
                c = json.loads(raw)
                found.append({"lat": float(c["lat"]), "lng": float(c["lng"]), "timestamp": c["timestamp"],
                              "at": datetime.fromisoformat(c["timestamp"])})
            except (ValueError, KeyError, TypeError):
                continue
        return found

    def replan_origin(self, trip_id: str, plan: list[dict], now: datetime | None = None) -> dict:
        """Where a new route starts: the traveller's newest check-in if it is recent, else the start of the step
        they are on."""
        now = now or self._clock()
        newest = next(iter(self._checkins(trip_id)), None)
        if newest and -60 <= (now - newest["at"]).total_seconds() <= self.RECENT_CHECKIN_S:   # a minute of clock skew
            return {"lat": newest["lat"], "lng": newest["lng"], "source": "checkin",
                    "at": newest["timestamp"], "label": "where the traveller last checked in"}
        current = min(self._current_step(trip_id), len(plan) - 1)
        start = plan[current].get("from") or {}
        return {"lat": start.get("lat"), "lng": start.get("lng"), "source": "step", "at": None,
                "label": f"{start['name']}, where their current step starts" if start.get("name")
                else "where their current step starts"}

    @staticmethod
    def _step_path(path: list, step: dict) -> list:
        """A step's line on the map: its piece of `path` (the route's, or of the steps it replaced)."""
        a, b = step.get("from") or {}, step.get("to") or {}
        if None in (a.get("lat"), a.get("lng"), b.get("lat"), b.get("lng")):
            return []
        return path_between(path, (a["lat"], a["lng"]), (b["lat"], b["lng"]))

    def _plan_paths(self, trip: dict, route: dict, plan: list[dict]) -> list:
        """Each of the plan's steps' lines on the map (stored when the plan changed; else cut from the route's)."""
        stored = trip.get("planPaths")
        if stored and len(stored) == len(plan):
            return stored
        return [self._step_path(route.get("path") or [], s) for s in plan]

    def trip_plan(self, trip_id: str, caller: Caller | None = None) -> dict:
        """The steps the traveller of a trip under way follows now (with a bridging bus or re-route sent), each with
        its line on the map, and `affected`: where those steps ride with no trains running (LTA's broken stretches),
        as lines to draw over them."""
        trip = self._stored_trip(trip_id, caller)
        if trip["status"] != "active":
            raise TripConflict("Only a trip that is under way has a plan to follow.")
        route, plan = self._active_plan(trip)
        paths = self._plan_paths(trip, route, plan)
        affected = self.planner.affected(plan, paths, self.planner.broken())
        steps = [{k: v for k, v in self.photos.step_with_urls(s).items() if k not in ("photoKey", "audioKey", "backup")} for s in plan]
        return {"tripId": trip_id, "planVersion": trip.get("planVersion") or 1, "steps": steps,
                "stepPaths": paths, "affected": affected}

    def trip_location(self, trip_id: str, caller: Caller | None = None) -> dict:
        """Where the traveller of a trip under way is, for the caregiver's map: their newest check-in however old
        (`at` says when) and the few before it (`trail`, oldest first), or, before their first check-in, the start
        of the step they are on (`source: "step"`). With the step they are on, as on their phone."""
        trip = self._stored_trip(trip_id, caller)
        if trip["status"] != "active":
            raise TripConflict("Only a trip that is under way has a traveller to follow.")
        _, plan = self._active_plan(trip)
        current = min(self._current_step(trip_id), len(plan) - 1)
        step = plan[current] if plan else {}
        checkins = self._checkins(trip_id)
        if checkins:
            where = {"lat": checkins[0]["lat"], "lng": checkins[0]["lng"], "at": checkins[0]["timestamp"], "source": "checkin"}
        else:
            start = step.get("from") or {}
            where = {"lat": start.get("lat"), "lng": start.get("lng"), "at": None, "source": "step"}
        return {"tripId": trip_id, **where, "trail": [[c["lat"], c["lng"]] for c in reversed(checkins)],
                "replanFrom": self.replan_origin(trip_id, plan) if plan else None,
                "stepIndex": current, "stepCount": len(plan),
                "step": {"mode": step.get("mode"), "instruction": step.get("instruction")} if step else None}

    def replan_options(self, trip_id: str, caller: Caller | None = None) -> dict:
        """A whole new route from where the traveller is to the destination: {from, options} (plan()'s options)."""
        trip, route, plan = self._owned_active_trip(trip_id, caller)
        origin = self.replan_origin(trip_id, plan)
        dest = route.get("destination") or {}
        if origin["lat"] is None or dest.get("lat") is None:
            raise TripConflict("Where the traveller is isn't known, so a new route can't be planned.")
        planned = self.plan((origin["lat"], origin["lng"]), (dest["lat"], dest["lng"]))
        # A ride through a breakdown would only get a bridging bus proposed again: leave those out if there is
        # another way (plan() adds bus-only routes when the breakdown hits every option).
        clear = [o for o in planned["options"] if not o["affected"]]
        if clear:
            planned = {**planned, "options": [{**o, "option": n} for n, o in enumerate(clear, start=1)]}
        return {"from": origin, **planned}

    def apply_replan(self, trip_id: str, steps: list[dict], caller: Caller | None = None,
                     step_paths: list | None = None) -> dict:
        """The caregiver sends one of replan_options()'s routes: it replaces the steps from the current one on."""
        trip, route, plan = self._owned_active_trip(trip_id, caller)
        start = min(self._current_step(trip_id), len(plan) - 1)
        worded = [{**s, "easyRead": t} for s, t in zip(steps, self._wording(plan, start, len(plan) - 1, steps, route))]
        offer = self._replan_offer(plan, start, worded, None)
        return self._public_trip(self._send_override(trip, route, plan, offer, by="caregiver", step_paths=step_paths))

    def send_route(self, trip_id: str, route_id: str, caller: Caller | None = None) -> dict:
        """The caregiver sends a route saved for the trip (planned from where the traveller is, or for a trip not started
        yet from its start; its wording and photos checked on Saved routes, then approved): its steps replace the
        traveller's from the current one on."""
        trip, route, plan = self._owned_active_trip(trip_id, caller, planned_too=True)
        chosen = self.get_route(route_id, caller)
        if caller is not None and not self._owns(caller, chosen):
            raise Forbidden("Only the route's caregiver can send it.")
        if chosen["travellerId"] != trip["travellerId"]:
            raise TripConflict("That route is for another traveller.")
        if not chosen["approved"]:
            raise TripConflict("Approve the new route on the Saved routes tab first.")
        start = min(self._current_step(trip_id), len(plan) - 1)
        offer = self._replan_offer(plan, start, chosen["steps"], route_id)
        paths = [self._step_path(chosen.get("path") or [], s) for s in chosen["steps"]]
        return self._public_trip(self._send_override(trip, route, plan, offer, by="caregiver", step_paths=paths))

    def _replan_offer(self, plan: list[dict], start: int, steps: list[dict], from_route: str | None) -> dict:
        """A new route from step `start` to the end (`fromRoute`: the saved route it came from, if any)."""
        return {"kind": "replan", "status": "pending", "line": None, "leaveAt": None, "target": None,
                "replacesFrom": start, "replacesTo": len(plan) - 1, "steps": steps, "fromRoute": from_route,
                "createdAt": iso_utc(self._clock()), "sendAt": None, "sentAt": None, "sentBy": None}

    @staticmethod
    def _clock_time(iso: str) -> str:
        return datetime.fromisoformat(iso).astimezone(SST).strftime("%H:%M")
