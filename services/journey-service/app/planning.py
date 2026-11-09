"""Route planning: OneMap's itineraries as contract steps, checked against LTA's live data.

    planner = Planner(settings, lta, onemap, clock, station_point)
    planner.plan(origin, destination, when, mode, ...)    {"options": [...], "sortedBy"} (POST /api/routes/plan)
    planner.check_disruptions(steps)                      the warnings for saved steps now
    planner.affected(steps, paths, segments)              where steps ride with no trains running (a breakdown)
    planner.bridges(steps, segments) / free_bus(...)      LTA's free buses past a breakdown

JourneyClient uses it as `client.planner` and keeps plan() and check_disruptions() for the API. The breakdown
helpers are also used by trip changes (trip_changes.py). `station_point` is the client's (station exits from
OneMap), passed in so a test can replace it on the client.
"""
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import bridge
from .config import Settings
from .easy_read import LINE_NAMES, route_wording
from .geo import EARTH_RADIUS_M, distance_m, distance_to_path_m, path_between
from .lta import LINE_CODES, SST, LtaClient, LtaError
from .onemap import OneMapClient, OneMapError, pretty
from .timefmt import iso_utc

log = logging.getLogger("journey")


ONEMAP_MODES = {"any": "TRANSIT", "bus": "BUS", "train": "RAIL"}   # Different modes for OneMap search and plan calls, from the API's `mode` query param.
CROWD_RANK = {None: 0, "low": 0, "moderate": 1, "high": 2}       # Crowding levels, for sorting and for the API's `crowd` field: None/low < moderate < high.
REALTIME_WITHIN = timedelta(minutes=20)           # How recent a bus arrival must be to be considered "realtime" (for the API's `realtime` field).
SORTS = {                                        # Sorting keys for the API's `sort` query param: a tuple of values for each plan, in order of priority.
    "time": lambda o: 0,
    "walking": lambda o: o["summary"]["walkDistanceM"],
    "crowding": lambda o: (CROWD_RANK[o["summary"]["crowd"]], sum(e.get("crowd") == "high" for e in o["live"]),
                           o["summary"]["walkDistanceM"]),
}
SERIOUS_INCIDENTS = {"Accident", "Road Block", "Fire", "Diversion", "Reverse Flow"}


def _iso(ms: int | None) -> str | None:
    """OneMap's milliseconds since 1970 as an API time."""
    return None if ms is None else iso_utc(datetime.fromtimestamp(ms / 1000, tz=timezone.utc))


def decode_polyline(encoded: str, precision: int = 5) -> list[tuple[float, float]]:
    """Decodes a Google/OpenTripPlanner encoded polyline into (lat, lng) pairs."""
    factor = 10 ** precision
    coords: list[tuple[float, float]] = []
    index = lat = lng = 0
    while index < len(encoded):
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lng += deltas[1]
        coords.append((lat / factor, lng / factor))
    return coords


def _near_box(path: list[tuple[float, float]], radius_m: float) -> Callable[[float, float], bool]:
    """A cheap first check before distance_to_path_m: is a point inside the path's bounding box, widened by
    `radius_m`? Points outside it can't be within `radius_m` of the path."""
    lats, lngs = [p[0] for p in path], [p[1] for p in path]
    dlat = radius_m / (math.pi * EARTH_RADIUS_M / 180)
    dlng = dlat / max(0.01, math.cos(math.radians(max(abs(min(lats)), abs(max(lats))))))
    lo_lat, hi_lat, lo_lng, hi_lng = min(lats) - dlat, max(lats) + dlat, min(lngs) - dlng, max(lngs) + dlng
    return lambda lat, lng: lo_lat <= lat <= hi_lat and lo_lng <= lng <= hi_lng


def _mentions_service(content: str, service: str) -> bool:
    return "bus" in content.lower() and bool(
        re.search(rf"(?<![\w/]){re.escape(service)}(?![\w/])", content))


def _train_disruptions(index: int, codes: set[str], alerts: dict) -> list[dict]:
    """A train_disruption warning for each LTA segment that touches one of the step's station codes."""
    warnings = []
    for seg in alerts["segments"]:
        overlap = sorted(codes & set(seg["stations"]))
        if overlap:
            message = alerts["messages"][0]["content"] if alerts["messages"] else f"Disruption on {seg['line']}"
            warnings.append({
                "type": "train_disruption",
                "severity": "high" if alerts["status"] == 2 else "info",
                "stepIndex": index, "message": message, "line": seg["line"], "affectedStations": overlap,
            })
    return warnings


def _bus_notices(index: int, service: str, alerts: dict) -> list[dict]:
    """A bus_notice for each LTA message that names the step's bus service."""
    return [{"type": "bus_notice", "severity": "info", "stepIndex": index, "message": m["content"], "serviceNo": service}
            for m in alerts["messages"] if _mentions_service(m["content"], service)]


# What check_disruptions() refreshes: the warnings a later LTA alert can add or take away.
DISRUPTION_WARNINGS = ("train_disruption", "bus_notice", "live_data_unavailable")
LIVE_DATA_UNAVAILABLE = {"type": "live_data_unavailable", "severity": "info", "stepIndex": None,
                         "message": "Live bus and disruption information is not available right now."}


class _LiveData:
    """Failure-tolerant LTA lookups for one plan() call, each made at most once. prefetch() makes the ones a plan
    needs in parallel; anything else is looked up on first use. After the first LTA failure the rest are skipped
    (the plan is `degraded` either way), so an LTA outage costs one timeout, not one per lookup."""

    PARALLEL = 8

    def __init__(self, client: "Planner"):
        self._c = client
        self._lta = client.plan_lta                 # no retries: live data is a bonus
        self.degraded = False
        self._alerts: dict | None = None
        self._incidents: list | None = None
        self._arrivals: dict = {}                    # (stop, service) -> arrival dict or None
        self._crowd: dict = {}                       # (line, when) -> {station: level}

    def _call(self, fn, *args):
        if self.degraded:
            return None
        try:
            return fn(*args)
        except LtaError as e:
            log.warning("LTA live data unavailable: %s", type(e).__name__)
            self.degraded = True
            return None

    def prefetch(self, lookups: list[tuple]) -> None:
        """Runs the given lookups together: ("alerts",), ("incidents",), ("bus", stop, service), ("crowd", line, when)."""
        jobs = {"alerts": self.alerts, "incidents": self.incidents, "bus": self.bus_arrival, "crowd": self.platform_crowd}
        unique = list(dict.fromkeys(lookups))
        if len(unique) <= 1:
            for kind, *args in unique:
                jobs[kind](*args)
            return
        with ThreadPoolExecutor(max_workers=min(self.PARALLEL, len(unique))) as pool:
            for f in [pool.submit(jobs[kind], *args) for kind, *args in unique]:
                f.result()

    def alerts(self) -> dict:
        if self._alerts is None:
            self._alerts = self._call(self._lta.train_alerts) or {"status": 1, "segments": [], "messages": []}
        return self._alerts

    def incidents(self) -> list[dict]:
        if self._incidents is None:
            self._incidents = self._call(self._lta.traffic_incidents) or []
        return self._incidents

    def bus_arrival(self, stop_code: str, service_no: str) -> dict | None:
        if (stop_code, service_no) not in self._arrivals:
            self._arrivals[(stop_code, service_no)] = self._call(self._lta.bus_arrival, stop_code, service_no)
        return self._arrivals[(stop_code, service_no)]

    def platform_crowd(self, line: str, when: datetime | None) -> dict:
        """{station: level} for a line: right now (when is None) or forecast for `when`. Crowding is a bonus, so
        a failure (including a key without that dataset) is logged and means "unknown", never `degraded`."""
        if (line, when) not in self._crowd:
            try:
                lta = self._lta
                self._crowd[(line, when)] = lta.platform_crowd(line) if when is None else lta.platform_crowd_forecast(line, when)
            except LtaError as e:
                log.warning("LTA platform crowding unavailable: %s", type(e).__name__)
                self._crowd[(line, when)] = {}
        return self._crowd[(line, when)]


class Planner:
    def __init__(self, settings: Settings, lta: LtaClient, onemap: OneMapClient, clock: Callable[[], datetime],
                 station_point: Callable[[dict], tuple[float, float] | None]):
        self._s = settings
        self.lta = lta
        self.plan_lta = lta.without_retries()        # live data in a plan is a bonus: never retried
        self._onemap = onemap
        self._clock = clock
        self._station_point = station_point

    # Planning
    #
    # Per itinerary: each leg becomes one step (WALK -> walk, BUS -> bus, SUBWAY -> train), matching
    # contracts/examples/route.json. `live` lists the next buses for every bus leg. `warnings` flags MRT
    # disruptions on the leg's stations, bus notices naming the leg's service, road incidents near the
    # bus path, and long walks. LTA being down never fails planning: a live_data_unavailable warning is
    # added instead.

    def plan(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        when: datetime | None = None,
        mode: str = "any",
        max_walk_m: int | None = None,
        max_options: int | None = None,
        sort_by: str = "walking",
    ) -> dict:
        """{"options": [...], "sortedBy": ...}. Raises OneMapError subclasses; LTA problems only add a warning.
        sort_by: "walking" (least walking first), "crowding" (least crowded, then least walking) or "time"
        (OneMap's own order); ties keep OneMap's order and the options are numbered after sorting."""
        if sort_by not in SORTS:
            raise ValueError(f"sort_by must be one of {sorted(SORTS)}")
        if when is None:
            when = self._clock().astimezone(SST)
        else:
            when = when.astimezone(SST) if when.tzinfo else when.replace(tzinfo=SST)
        raw = self._onemap.route_pt(origin, destination, when, mode=ONEMAP_MODES[mode],
                            max_walk_m=max_walk_m, num_itineraries=max_options)
        itineraries = (raw.get("plan") or {}).get("itineraries") or []

        live = _LiveData(self)
        live.prefetch([lookup for it in itineraries for leg in it.get("legs", []) for lookup in self._lookups(leg)])
        walk_limit = max_walk_m or self._s.plan_max_walk_m
        options = [self._option(n, it, live, walk_limit) for n, it in enumerate(itineraries, start=1)]
        if live.degraded:
            for opt in options:
                opt["warnings"].append(dict(LIVE_DATA_UNAVAILABLE))
        # A breakdown: each option says where it rides with no trains (`affected`) and the free bus past it
        # (`bridges`, for the caregiver to see, never saved). Those go last; if it hits them all, buses only too.
        alerts = live.alerts()
        broken = alerts["segments"] if alerts["status"] >= self._s.disruption_min_status else []
        for opt in options:
            opt["affected"] = self.affected(opt["steps"], opt["stepPaths"], broken)
            opt["bridges"] = self.bridges(opt["steps"], broken) if opt["affected"] else []
        if broken and mode == "any" and options and all(o["affected"] for o in options):
            try:
                options += [o for o in self.plan(origin, destination, when, "bus", max_walk_m, max_options, sort_by)["options"]
                            if not o["affected"]]
            except OneMapError as e:
                log.warning("plan: no bus-only routes round the breakdown (%s)", type(e).__name__)
        options = sorted(options, key=lambda o: (bool(o["affected"]), SORTS[sort_by](o)))   # stable: ties keep OneMap's order
        for n, opt in enumerate(options, start=1):
            opt["option"] = n
        return {"options": options, "sortedBy": sort_by}

    def check_disruptions(self, steps: list[dict]) -> dict:
        """The disruption warnings for route steps as LTA reports things now: {"warnings": [...], "checkedAt"}.

        For steps already planned or saved (a plan option, a saved route, a trip's route), so a caregiver sees an MRT
        fault or bus notice that started after planning. Same warnings as plan() (types in DISRUPTION_WARNINGS),
        matched the same way: a train step by its station codes (`stops`, else its two ends), a bus step by its
        service number (`line`). Costs one cached LTA call (train alerts), whatever the number of steps."""
        live = _LiveData(self)
        warnings: list[dict] = []
        for step in steps:
            index, mode = step.get("stepIndex"), step.get("mode")
            if mode == "train":
                stops = step.get("stops") or [step.get("from") or {}, step.get("to") or {}]
                codes = {s.get("code") for s in stops if s.get("code")}
                if codes:
                    warnings += _train_disruptions(index, codes, live.alerts())
            elif mode == "bus" and step.get("line"):
                warnings += _bus_notices(index, step["line"], live.alerts())
        if live.degraded:
            warnings.append(dict(LIVE_DATA_UNAVAILABLE))
        return {"warnings": warnings, "checkedAt": iso_utc(self._clock())}

    @staticmethod
    def _point(p: dict) -> dict:
        return {"name": pretty(p.get("name")), "code": p.get("stopCode"), "lat": p.get("lat"), "lng": p.get("lon")}

    @classmethod
    def _step(cls, index: int, leg: dict) -> dict:
        mode = leg.get("mode")
        frm, to = cls._point(leg["from"]), cls._point(leg["to"])
        here, there = frm["name"] or "here", to["name"] or "your destination"
        stops = len(leg.get("intermediateStops") or []) + 1
        line, underground = None, False
        if mode == "BUS":
            step_mode, line = "bus", leg.get("route") or None
            instruction = f"Take bus {line} from {here} to {there} ({stops} stops)"
        elif mode == "SUBWAY":
            step_mode, line = "train", leg.get("route") or None
            underground = None                # OneMap doesn't say which MRT stretches are underground
            instruction = f"Take the {LINE_NAMES.get(line, f'{line} line')} from {here} to {there} ({stops} stops)"
        else:
            step_mode = "walk"
            metres = int(round(leg.get("distance", 0) / 10) * 10)
            instruction = f"Walk {f'{metres} m' if metres >= 10 else 'a few steps'} to {there}"
        step = {
            "stepIndex": index, "mode": step_mode, "instruction": instruction, "easyRead": None,
            "from": frm, "to": to, "line": line, "expectedDurationS": leg.get("duration"),
            "underground": underground, "photoKey": None, "audioKey": None, "backup": None,
        }
        # Extra: info on how many stops to ride, which easy-read wording uses, and
        # every stop on the leg: for a train, so check_disruptions() can tell whether an MRT fault falls between
        # the two ends and easy_read() which way it goes; for a bus, the stop before getting off ("After
        # Block 301, press the bell").
        if step_mode in ("bus", "train"):
            step["stopCount"] = stops
            every = [leg["from"], *(leg.get("intermediateStops") or []), leg["to"]]
            step["stops"] = [{"code": s.get("stopCode"), "name": pretty(s.get("name"))} for s in every]
        return step                     # easyRead: _option() writes it for the whole route (route_wording)

    def _with_bus_service(self, step: dict) -> dict:
        """A bus step with where the bus is going (`towards`, its front sign) and how often it comes (`every`, in
        minutes), from LTA's static lists. Best-effort."""
        board, alight = (step.get("from") or {}).get("code"), (step.get("to") or {}).get("code")
        if step["mode"] != "bus" or not (step.get("line") and board and alight):
            return step
        found = self.lta.bus_service(step["line"], board, alight)
        if not found:
            return step
        return {**step, "towards": pretty(found["towards"]), "every": found["every"]}

    @staticmethod
    def _leg_path(leg: dict) -> list[tuple[float, float]]:
        points = (leg.get("legGeometry") or {}).get("points")
        path = decode_polyline(points) if points else []
        return path or [(leg["from"]["lat"], leg["from"]["lon"]), (leg["to"]["lat"], leg["to"]["lon"])]

    @staticmethod
    def _leg_stop_codes(leg: dict) -> set[str]:
        codes = {leg["from"].get("stopCode"), leg["to"].get("stopCode")}
        codes.update(s.get("stopCode") for s in leg.get("intermediateStops") or [])
        codes.discard(None)
        return codes

    def _lookups(self, leg: dict) -> list[tuple]:
        """The LTA lookups _check_leg will make for this leg, for _LiveData.prefetch."""
        mode = leg.get("mode")
        if mode == "SUBWAY":
            crowd = self._crowd_lookup(leg)
            return [("alerts",)] + ([("crowd", crowd[0], crowd[1])] if crowd else [])
        if mode == "BUS":
            service, stop = leg.get("route"), leg["from"].get("stopCode")
            return ([("bus", stop, service), ("alerts",)] if service and stop else []) + [("incidents",)]
        return []

    def _check_leg(self, index: int, leg: dict, live: _LiveData, path: list[tuple[float, float]]) -> tuple[list, list]:
        """Returns (live entries, warnings) for one leg; `path` is the leg's decoded line."""
        entries: list[dict] = []
        warnings: list[dict] = []
        mode = leg.get("mode")

        if mode == "SUBWAY":
            warnings += _train_disruptions(index, self._leg_stop_codes(leg), live.alerts())
            entry = self._platform_crowd(index, leg, live)
            if entry:
                entries.append(entry)
                if entry["crowd"] == "high":
                    name = pretty(leg["from"].get("name")) or entry["station"]
                    when = "now" if entry["source"] == "real-time" else "at this time"
                    warnings.append({"type": "crowded", "severity": "info", "stepIndex": index,
                                     "message": f"{name} is very crowded {when}.", "station": entry["station"]})

        elif mode == "BUS":
            service, stop = leg.get("route"), leg["from"].get("stopCode")
            if service and stop:
                arrival = live.bus_arrival(stop, service)
                if arrival is not None:
                    svc = next((s for s in arrival["services"] if s["serviceNo"] == service), None)
                    buses = svc["buses"] if svc else []
                    entries.append({
                        "stepIndex": index, "busStopCode": stop, "serviceNo": service,
                        "crowd": buses[0]["crowd"] if buses else None,          # how full the next bus is
                        "nextBuses": [
                            {"minutes": b["minutes"], "display": b["display"], "load": b["load"], "crowd": b["crowd"],
                             "wheelchairAccessible": b["wheelchairAccessible"]}
                            for b in buses],
                    })
                    if entries[-1]["crowd"] == "high":
                        warnings.append({"type": "crowded", "severity": "info", "stepIndex": index, "serviceNo": service,
                                         "message": f"Bus {service} is very full right now. You may have to stand."})
                warnings += _bus_notices(index, service, live.alerts())

            near = _near_box(path, self._s.incident_radius_m)
            for inc in live.incidents():
                if inc["lat"] is None or inc["lng"] is None or not near(inc["lat"], inc["lng"]):
                    continue
                d = distance_to_path_m(inc["lat"], inc["lng"], path)
                if d <= self._s.incident_radius_m:
                    warnings.append({
                        "type": "traffic_incident",
                        "severity": "warning" if inc["type"] in SERIOUS_INCIDENTS else "info",
                        "stepIndex": index, "message": inc["message"], "incidentType": inc["type"],
                        "distanceM": round(d),
                    })
        return entries, warnings

    def _crowd_lookup(self, leg: dict) -> tuple[str, datetime | None] | None:
        """(DataMall line, None for right now / the time to forecast for) for a train leg, or None if unknown."""
        line = LINE_CODES.get((leg.get("route") or "").upper())
        if not (leg["from"].get("stopCode") and line):
            return None
        start = datetime.fromtimestamp(leg["startTime"] / 1000, tz=timezone.utc) if leg.get("startTime") else None
        realtime = start is None or start - self._clock() <= REALTIME_WITHIN
        return line, None if realtime else start

    def _platform_crowd(self, index: int, leg: dict, live: _LiveData) -> dict | None:
        """How busy the platform is where this train leg starts: LTA's real-time level when the leg starts soon,
        its forecast for that time when it starts later. None when the line is unknown or LTA has no level."""
        lookup = self._crowd_lookup(leg)
        if lookup is None:
            return None
        station = leg["from"]["stopCode"]
        level = live.platform_crowd(*lookup).get(station)
        if level is None:
            return None
        return {"stepIndex": index, "station": station, "line": leg["route"], "crowd": level,
                "source": "real-time" if lookup[1] is None else "forecast"}

    def _option(self, number: int, itinerary: dict, live: _LiveData, walk_limit_m: int) -> dict:
        steps, path, step_paths, live_entries, warnings = [], [], [], [], []
        for i, leg in enumerate(itinerary.get("legs", [])):
            steps.append(self._with_bus_service(self._step(i, leg)))
            leg_path = self._leg_path(leg)                   # decoded once: drawn here, checked in _check_leg
            pts = [list(pt) for pt in leg_path]
            # OneMap's legs can end up to ~80 m apart (a bus stop sits off the snapped road). Start each
            # step where the previous one ended so the drawn route has no break; that short connector
            # takes the later step's colour.
            if step_paths and step_paths[-1][-1] != pts[0]:
                pts.insert(0, step_paths[-1][-1])
            step_paths.append(pts)
            # OneMap's maxWalkDistance isn't enforced (a 300 m limit still returned 1 km walks),
            # so long walks are flagged here instead.
            if leg.get("mode") == "WALK" and leg.get("distance", 0) > walk_limit_m:
                metres = int(round(leg["distance"] / 10) * 10)
                warnings.append({
                    "type": "long_walk", "severity": "info", "stepIndex": i, "distanceM": metres,
                    "message": f"This walk is about {metres} m, longer than {walk_limit_m} m."})
            for pt in pts:
                if not path or path[-1] != pt:
                    path.append(pt)
            entries, warns = self._check_leg(i, leg, live, leg_path)
            live_entries += entries
            warnings += warns

        # The wording again, now each step knows the one before it (and the buses where they are going).
        for step, text in zip(steps, route_wording(steps)):
            step["easyRead"] = text

        total_walk = itinerary.get("walkDistance", 0)
        if total_walk > 2 * walk_limit_m:                 # a few short walks can still add up to too much
            warnings.append({
                "type": "long_total_walk", "severity": "info", "stepIndex": None,
                "distanceM": int(round(total_walk / 10) * 10),
                "message": f"Altogether you will walk about {int(round(total_walk / 10) * 10)} m, "
                           f"more than {2 * walk_limit_m} m."})
        crowds = [e["crowd"] for e in live_entries if e.get("crowd")]
        try:
            fare = float(itinerary["fare"])
        except (KeyError, TypeError, ValueError):
            fare = None
        return {
            "option": number,
            "summary": {
                "durationS": itinerary.get("duration"),
                "startTime": _iso(itinerary.get("startTime")),
                "endTime": _iso(itinerary.get("endTime")),
                "walkDistanceM": round(itinerary.get("walkDistance", 0)),
                "transfers": itinerary.get("transfers"),
                "fare": fare,
                "crowd": max(crowds, key=CROWD_RANK.get) if crowds else None,     # busiest leg: low/moderate/high
                "modes": list(dict.fromkeys(s["mode"] for s in steps)),
            },
            # `stepPaths[i]` is the [lat, lng] path of steps[i]; `path` is all of them joined (the contract field).
            "steps": steps, "path": path, "stepPaths": step_paths, "live": live_entries, "warnings": warnings,
        }

    FREE_BUS_STOP_M = 300                # a bus stop this close to a station counts as at the station

    def free_bus(self, leave: dict, target: dict) -> dict | None:
        """An ordinary bus (free during the breakdown) from a stop near one station to a stop near the other."""
        near = {}
        for name, station in (("leave", leave), ("target", target)):
            if station.get("lat") is None:
                return None
            here = (station["lat"], station["lng"])
            near[name] = {s["code"]: s for s in self.lta.bus_stops()
                          if s.get("lat") is not None and distance_m(here, (s["lat"], s["lng"])) <= self.FREE_BUS_STOP_M}
        options = self.lta.services_between(set(near["leave"]), set(near["target"]))
        if not options:
            return None
        service, board, alight, _ = options[0]
        def stop(s):
            return {"code": s["code"], "name": pretty(s.get("description")), "lat": s.get("lat"), "lng": s.get("lng")}
        return {"serviceNo": service, "board": stop(near["leave"][board]), "alight": stop(near["target"][alight])}

    def broken(self) -> list[dict]:
        """LTA's broken stretches now (TrainServiceAlerts segments); none if LTA can't be asked."""
        try:
            alerts = self.lta.train_alerts()
        except LtaError as e:
            log.warning("no train alerts (%s)", type(e).__name__)
            return []
        return alerts["segments"] if alerts["status"] >= self._s.disruption_min_status else []

    def stop_point(self, stop: dict) -> tuple[float, float] | None:
        return (stop["lat"], stop["lng"]) if stop.get("lat") is not None else self._station_point(stop)

    def affected(self, steps: list[dict], paths: list, segments: list[dict]) -> list[dict]:
        """Where `steps` ride with no trains running (LTA's broken `segments`): from the station before the broken
        ones to the one after, with that piece of the step's line (`paths[i]`) to draw over it."""
        found = []
        for segment in segments:
            for i, step in enumerate(steps):
                span = bridge.through(step, set(segment["stations"]))
                if span is None:
                    continue
                first, last = span
                line = paths[i] if i < len(paths) else []
                ends = [self.stop_point(first), self.stop_point(last)]
                found.append({"stepIndex": i, "line": segment.get("line"), "from": first.get("name"),
                              "to": last.get("name"), "path": path_between(line, *ends) if all(ends) and line else line,
                              "message": f"No trains between {first.get('name')} and {last.get('name')}"})
        return found

    def bridges(self, steps: list[dict], segments: list[dict]) -> list[dict]:
        """The free bus past each broken stretch `steps` ride through (bridge.bridge_for), to show with a plan."""
        found = []
        for segment in segments:
            for i, step in enumerate(steps):
                if bridge.through(step, set(segment["stations"])) is None:
                    continue
                try:
                    made = bridge.bridge_for(step, segment, self.free_bus, self._station_point)
                except LtaError as e:
                    log.warning("plan: no free buses (%s)", type(e).__name__)
                    made = None
                if made:
                    ride = next(s for s in made["steps"] if s.get("bridge"))
                    found.append({"stepIndex": i, "kind": made["kind"], "busNo": ride["bridge"].get("serviceNo"),
                                  "leaveAt": made["leaveAt"].get("name"), "target": made["target"].get("name"),
                                  "steps": [{"mode": s["mode"], "instruction": s["instruction"]} for s in made["steps"]]})
        return found
