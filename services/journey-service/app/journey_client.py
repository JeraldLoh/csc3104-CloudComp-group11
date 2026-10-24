"""JourneyClient: the API's logic in one class (proof of concept): planning, saved routes, trips and packs.

    client = JourneyClient(settings, redis_client=redis)

    client.search_places("Tampines Mall")        OneMap place/address search
    client.plan(origin, destination, ...)        OneMap itineraries -> contract steps, checked against LTA
    client.create_route / create_trip / ...      saved routes, trips, route packs
    client.bus_arrival("83139")                  LTA data, through the shared LtaClient in client.lta

The LTA client lives in lta.py and the disruption checker in disruption.py: they are shared with / run by the
Lambda, which must not need this file's login, route and pack code.

Failures raise LtaError / OneMapError subclasses; plan() degrades instead when only LTA is down.
Redis is optional here (a cache for lookups; stores for routes and trips when no DynamoDB table is configured).
"""
import json
import logging
import math
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx

from .auth import Caller, Forbidden
from .cache import JsonCache
from .config import Settings
from .easy_read import LINE_NAMES, easy_read, with_easy_read
from .lta import LINE_CODES, SST, LtaClient, LtaError
from .onemap_token import OneMapToken
from .pack_store import PackUnavailable
from .route_store import RedisRouteStore, RouteExists, RouteInvalid, RouteNotFound
from .trip_store import RedisTripStore, TripConflict, TripExists, TripNotFound

log = logging.getLogger("journey")

EARTH_RADIUS_M = 6_371_000.0

ONEMAP_MODES = {"any": "TRANSIT", "bus": "BUS", "train": "RAIL"}   # plan() `mode` -> OneMap `mode`
CROWD_RANK = {None: 0, "low": 0, "moderate": 1, "high": 2}       # unknown counts as low when comparing options
REALTIME_WITHIN = timedelta(minutes=20)           # a train leg starting later than this uses the crowd forecast
SORTS = {                                        # plan(sort_by=...) -> sort key over an option
    "time": lambda o: 0,
    "walking": lambda o: o["summary"]["walkDistanceM"],
    "crowding": lambda o: (CROWD_RANK[o["summary"]["crowd"]], sum(e.get("crowd") == "high" for e in o["live"]),
                           o["summary"]["walkDistanceM"]),
}
SERIOUS_INCIDENTS = {"Accident", "Road Block", "Fire", "Diversion", "Reverse Flow"}

# Standard texts for the traveller's phone (contracts/examples/route-pack.json).
PACK_MESSAGES = {
    "somethingChanged": "Something has changed. Stay where you are. Your caregiver has been told.",
    "offRoute": "You may be going the wrong way. Stop and look at the next picture.",
    "help": "Press the big red button if you need help.",
}


# --- errors ----------------------------------------------------------------------------

class StorageNotReady(Exception):
    """A store the service needs can't be used. `label` says which one, `code` is the AWS error code
    (e.g. ResourceNotFoundException, AccessDeniedException) and nothing more, so /ready can show it safely."""

    def __init__(self, label: str, code: str):
        super().__init__(f"{label}: {code}")
        self.label, self.code = label, code


class OneMapError(Exception):
    pass


class OneMapUnavailable(OneMapError):
    """OneMap is down, slow or rate-limiting us."""


class OneMapAuthError(OneMapError):
    """The token is missing, expired or rejected."""


# --- helpers ---------------------------------------------------------------------------

def titlecase(name: str | None) -> str | None:
    """'PUNGGOL MRT STATION' -> 'Punggol MRT Station'."""
    if not name:
        return None
    return re.sub(r"\b(Mrt|Lrt)\b", lambda m: m.group(1).upper(), name.title())


def pretty(name: str | None) -> str | None:
    """Like titlecase, but OneMap's 'Origin'/'Destination' placeholders become None."""
    return None if name in ("Origin", "Destination") else titlecase(name)


def _clean(value: str | None) -> str | None:
    """OneMap fills missing fields with 'NIL' or ''."""
    value = (value or "").strip()
    return None if value.upper() in ("", "NIL") else value


def _iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _to_xy(lat: float, lng: float, ref_lat: float) -> tuple[float, float]:
    """Local flat-earth projection in metres; accurate enough over a few kilometres."""
    return (math.radians(lng) * EARTH_RADIUS_M * math.cos(math.radians(ref_lat)),
            math.radians(lat) * EARTH_RADIUS_M)


def distance_to_path_m(lat: float, lng: float, path: list[tuple[float, float]]) -> float:
    """Shortest distance in metres from a point to a polyline (inf for an empty path)."""
    if not path:
        return math.inf
    px, py = _to_xy(lat, lng, lat)
    if len(path) == 1:
        ax, ay = _to_xy(*path[0], lat)
        return math.hypot(px - ax, py - ay)
    best = math.inf
    for (alat, alng), (blat, blng) in zip(path, path[1:]):
        ax, ay = _to_xy(alat, alng, lat)
        bx, by = _to_xy(blat, blng, lat)
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
        best = min(best, math.hypot(px - (ax + t * dx), py - (ay + t * dy)))
    return best


def _mentions_service(content: str, service: str) -> bool:
    return "bus" in content.lower() and bool(
        re.search(rf"(?<![\w/]){re.escape(service)}(?![\w/])", content))


class _LiveData:
    """Lazy, failure-tolerant LTA lookups for one plan() call."""

    def __init__(self, client: "JourneyClient"):
        self._c = client
        self.degraded = False
        self._alerts: dict | None = None
        self._incidents: list | None = None
        self._crowd: dict = {}                       # (line, when) -> {station: level}, one lookup per plan() call

    def _call(self, fn, *args):
        try:
            return fn(*args)
        except LtaError as e:
            log.warning("LTA live data unavailable: %s", type(e).__name__)
            self.degraded = True
            return None

    def alerts(self) -> dict:
        if self._alerts is None:
            self._alerts = self._call(self._c.train_alerts) or {"status": 1, "segments": [], "messages": []}
        return self._alerts

    def incidents(self) -> list[dict]:
        if self._incidents is None:
            self._incidents = self._call(self._c.traffic_incidents) or []
        return self._incidents

    def bus_arrival(self, stop_code: str, service_no: str) -> dict | None:
        return self._call(self._c.bus_arrival, stop_code, service_no)

    def platform_crowd(self, line: str, when: datetime | None) -> dict:
        """{station: level} for a line: right now (when is None) or forecast for `when`. Crowding is a bonus, so
        a failure (including a key without that dataset) is logged and means "unknown", never `degraded`."""
        if (line, when) not in self._crowd:
            try:
                lta = self._c.lta
                self._crowd[(line, when)] = lta.platform_crowd(line) if when is None else lta.platform_crowd_forecast(line, when)
            except LtaError as e:
                log.warning("LTA platform crowding unavailable: %s", type(e).__name__)
                self._crowd[(line, when)] = {}
        return self._crowd[(line, when)]


# --- the client ------------------------------------------------------------------------

class JourneyClient:
    def __init__(
        self,
        settings: Settings,
        redis_client=None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        lta_http: httpx.Client | None = None,
        onemap_http: httpx.Client | None = None,
        route_store=None,
        trip_store=None,
        pack_store=None,
        lta: LtaClient | None = None,
    ):
        self._s = settings
        self._r = redis_client
        # Saved routes and trips: DynamoDB, or Redis when no table is configured (see route_store.py).
        self._routes = route_store or (RedisRouteStore(redis_client) if redis_client is not None else None)
        self._trips = trip_store or (RedisTripStore(redis_client) if redis_client is not None else None)
        self._packs = pack_store                     # S3; None = packs unavailable
        self._storage_ok_until = 0.0                 # time.monotonic() until which storage_ping() is skipped
        self._sleep = sleep
        self._clock = clock
        self._cache = JsonCache(redis_client)
        # LTA data comes from the shared client; the API calls it through the methods below.
        self.lta = lta or LtaClient(settings, redis_client=redis_client, sleep=sleep, clock=clock, http=lta_http)
        self._recorded = onemap_http is None and settings.onemap_mode == "recorded"
        if self._recorded:
            from .replay import RecordedOneMap       # only loaded in saved-data mode (see replay.py)
            onemap_http = httpx.Client(base_url=settings.onemap_base_url, transport=RecordedOneMap())
        self._onemap_http = onemap_http or httpx.Client(
            base_url=settings.onemap_base_url, timeout=settings.onemap_timeout_s)
        # Logs in with the OneMap account when there is one (saved data needs no token at all).
        self._token = None if self._recorded else OneMapToken(settings, redis_client, clock=clock)

    # ===== OneMap transport =====

    def _onemap_get(self, path: str, params: dict, auth: bool = True) -> dict:
        headers = {}
        if auth and not self._recorded:
            token = self._token.get() or self._s.onemap_api_key          # the account's token, else the pasted one
            if not token:
                raise OneMapAuthError("No OneMap token: set ONEMAP_API_EMAIL and ONEMAP_API_PASSWORD, or ONEMAP_API_KEY")
            headers["Authorization"] = f"Bearer {token}"
        last: Exception | None = None
        renewed = False
        for attempt in range(self._s.onemap_retries + 1):
            if attempt:
                self._sleep(0.5 * 2 ** (attempt - 1))
            try:
                r = self._onemap_http.get(path, params=params, headers=headers)
            except httpx.HTTPError as e:
                last = e
                continue
            if r.status_code in (401, 403) and auth and not self._recorded and self._token.enabled and not renewed:
                renewed = True                       # it expired (or was revoked) early: log in again, once
                fresh = self._token.get(refresh=True)
                if fresh:
                    headers["Authorization"] = f"Bearer {fresh}"
                    try:
                        r = self._onemap_http.get(path, params=params, headers=headers)
                    except httpx.HTTPError as e:
                        last = e
                        continue
            if r.status_code in (401, 403):
                raise OneMapAuthError(f"OneMap rejected the token ({r.status_code}); it may have expired")
            if r.status_code == 429 or r.status_code >= 500:
                last = OneMapUnavailable(f"OneMap returned {r.status_code}")
                continue
            if r.status_code >= 400:
                raise OneMapError(f"OneMap returned {r.status_code} for {path}")
            try:
                return r.json()
            except ValueError as e:
                last = e
        log.warning("OneMap %s failed after %d attempts: %s", path, self._s.onemap_retries + 1, type(last).__name__)
        raise OneMapUnavailable("OneMap is not responding") from last

    # ===== LTA DataMall (the work is done by self.lta, see lta.py) =====

    def bus_stops(self) -> list[dict]:
        return self.lta.bus_stops()

    def bus_stop(self, code: str) -> dict | None:
        return self.lta.bus_stop(code)

    def bus_arrival(self, bus_stop_code: str, service_no: str | None = None) -> dict:
        return self.lta.bus_arrival(bus_stop_code, service_no)

    def train_alerts(self) -> dict:
        return self.lta.train_alerts()

    def traffic_incidents(self) -> list[dict]:
        return self.lta.traffic_incidents()

    # ===== OneMap =====

    def search_places(self, query: str, limit: int = 10) -> list[dict]:
        """Place/address search: [{name, address, postalCode, lat, lng}]. Needs no token."""
        key = f"{'recorded:' if self._recorded else ''}places:{query.strip().lower()}"   # saved and live never mix
        results = self._cache.cached(key, self._s.places_ttl_s, lambda: (
            self._onemap_get("/api/common/elastic/search", {
                "searchVal": query, "returnGeom": "Y", "getAddrDetails": "Y", "pageNum": "1"},
                auth=False).get("results") or []))
        places = []
        for r in results:
            try:
                lat, lng = float(r["LATITUDE"]), float(r["LONGITUDE"])
            except (KeyError, TypeError, ValueError):
                continue
            places.append({
                "name": titlecase(_clean(r.get("BUILDING")) or _clean(r.get("SEARCHVAL")) or _clean(r.get("ADDRESS"))),
                "address": titlecase(_clean(r.get("ADDRESS"))),
                "postalCode": _clean(r.get("POSTAL")),
                "lat": lat,
                "lng": lng,
            })
        return places[:limit]

    def route_pt(self, start: tuple[float, float], end: tuple[float, float], when: datetime,
                 mode: str = "TRANSIT", max_walk_m: int | None = None, num_itineraries: int | None = None) -> dict:
        """OneMap's raw public-transport itineraries: plan.itineraries[].legs[] (see tests/fixtures)."""
        return self._onemap_get("/api/public/routingsvc/route", {
            "start": f"{start[0]},{start[1]}",
            "end": f"{end[0]},{end[1]}",
            "routeType": "pt",
            "date": when.strftime("%m-%d-%Y"),                # OneMap wants MM-DD-YYYY
            "time": when.strftime("%H:%M:%S"),
            "mode": mode,
            "maxWalkDistance": str(max_walk_m or self._s.plan_max_walk_m),
            "numItineraries": str(num_itineraries or self._s.plan_options),
        })

    # ===== planning: OneMap itineraries -> contract steps, checked against LTA =====
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
        raw = self.route_pt(origin, destination, when, mode=ONEMAP_MODES[mode],
                            max_walk_m=max_walk_m, num_itineraries=max_options)
        itineraries = (raw.get("plan") or {}).get("itineraries") or []

        live = _LiveData(self)
        walk_limit = max_walk_m or self._s.plan_max_walk_m
        options = [self._option(n, it, live, walk_limit) for n, it in enumerate(itineraries, start=1)]
        if live.degraded:
            for opt in options:
                opt["warnings"].append({
                    "type": "live_data_unavailable", "severity": "info", "stepIndex": None,
                    "message": "Live bus and disruption information is not available right now."})
        options = sorted(options, key=SORTS[sort_by])           # stable: ties keep OneMap's order
        for n, opt in enumerate(options, start=1):
            opt["option"] = n
        return {"options": options, "sortedBy": sort_by}

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
        # Extras (not in contracts/examples/route.json): how many stops to ride, which easy-read wording uses.
        if step_mode in ("bus", "train"):
            step["stopCount"] = stops
        if step_mode == "train":
            # Every station on the leg, so check_disruptions() can tell whether an MRT fault falls between the two ends.
            every = [leg["from"], *(leg.get("intermediateStops") or []), leg["to"]]
            step["stops"] = [{"code": s.get("stopCode"), "name": pretty(s.get("name"))} for s in every]
        step["easyRead"] = easy_read(step)
        return step

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

    def _check_leg(self, index: int, leg: dict, live: _LiveData) -> tuple[list, list]:
        """Returns (live entries, warnings) for one leg."""
        entries: list[dict] = []
        warnings: list[dict] = []
        mode = leg.get("mode")

        if mode == "SUBWAY":
            alerts = live.alerts()
            codes = self._leg_stop_codes(leg)
            for seg in alerts["segments"]:
                overlap = sorted(codes & set(seg["stations"]))
                if overlap:
                    message = alerts["messages"][0]["content"] if alerts["messages"] else f"Disruption on {seg['line']}"
                    warnings.append({
                        "type": "train_disruption",
                        "severity": "high" if alerts["status"] == 2 else "info",
                        "stepIndex": index, "message": message, "line": seg["line"], "affectedStations": overlap,
                    })
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
                for m in live.alerts()["messages"]:
                    if _mentions_service(m["content"], service):
                        warnings.append({"type": "bus_notice", "severity": "info", "stepIndex": index,
                                         "message": m["content"], "serviceNo": service})

            path = self._leg_path(leg)
            for inc in live.incidents():
                if inc["lat"] is None or inc["lng"] is None:
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

    def _platform_crowd(self, index: int, leg: dict, live: _LiveData) -> dict | None:
        """How busy the platform is where this train leg starts: LTA's real-time level when the leg starts soon,
        its forecast for that time when it starts later. None when the line is unknown or LTA has no level."""
        station = leg["from"].get("stopCode")
        line = LINE_CODES.get((leg.get("route") or "").upper())
        if not (station and line):
            return None
        start = datetime.fromtimestamp(leg["startTime"] / 1000, tz=timezone.utc) if leg.get("startTime") else None
        realtime = start is None or start - self._clock() <= REALTIME_WITHIN
        level = live.platform_crowd(line, None if realtime else start).get(station)
        if level is None:
            return None
        return {"stepIndex": index, "station": station, "line": leg["route"], "crowd": level,
                "source": "real-time" if realtime else "forecast"}

    def _option(self, number: int, itinerary: dict, live: _LiveData, walk_limit_m: int) -> dict:
        steps, path, step_paths, live_entries, warnings = [], [], [], [], []
        for i, leg in enumerate(itinerary.get("legs", [])):
            steps.append(self._step(i, leg))
            pts = [list(pt) for pt in self._leg_path(leg)]
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
            entries, warns = self._check_leg(i, leg, live)
            live_entries += entries
            warnings += warns

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

    # ===== saved routes (contracts/api.md: /api/routes) =====
    #
    # A route is route.json: the caregiver's chosen plan option plus names. `version` goes up by one on
    # every edit and an edit clears `approved`, so a caregiver must re-approve what they changed. Approving
    # doesn't change the content, so it keeps the version (a trip pins `routeVersion`). Two people editing at
    # once can't overwrite each other: update_route needs the version you edited, else it raises RouteConflict.

    def _route_store(self):
        if self._routes is None:
            raise RuntimeError("saved routes need a Redis client (or another route store)")
        return self._routes

    def storage_ping(self) -> None:
        """Raises StorageNotReady (naming the store and the AWS error code) if the routes table, trips table or pack
        bucket can't be used. Redis stores have no ping (the Redis check covers them). A success is remembered
        for READY_CACHE_S seconds; a failure never is."""
        if time.monotonic() < self._storage_ok_until:
            return
        for label, store in (("routes table", self._routes), ("trips table", self._trips), ("pack bucket", self._packs)):
            ping = getattr(store, "ping", None)
            if not ping:
                continue
            try:
                ping()
            except Exception as e:  # noqa: BLE001 - whatever the AWS client raised, report which store and why
                code = getattr(e, "response", {}).get("Error", {}).get("Code") or type(e).__name__
                raise StorageNotReady(label, str(code)) from e
        self._storage_ok_until = time.monotonic() + self._s.ready_cache_s

    def _now_iso(self) -> str:
        return self._clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Who may touch what. Every public route/trip method takes an optional `caller` (auth.Caller); None means
    # auth is off and nothing is checked. A route belongs to its caregiver (full control) and its traveller
    # (read, and run their own trips). Anyone else gets "not found", so they can't tell a route exists.
    # Not checked yet: that the caregiver is really linked to the traveller; that needs the Profiles service.

    @staticmethod
    def _owns(caller: Caller, route: dict) -> bool:
        return caller.is_caregiver and route["caregiverId"] == caller.id

    @staticmethod
    def _participates(caller: Caller, route: dict) -> bool:
        return route["caregiverId" if caller.is_caregiver else "travellerId"] == caller.id

    def _may_see_trip(self, caller: Caller, trip: dict, routes: dict | None = None) -> bool:
        if not caller.is_caregiver:
            return trip["travellerId"] == caller.id
        route = (routes if routes is not None else {}).get(trip["routeId"]) or self._route_store().get(trip["routeId"])
        if routes is not None and route is not None:
            routes[trip["routeId"]] = route
        return route is not None and route["caregiverId"] == caller.id

    def create_route(self, data: dict, caller: Caller | None = None) -> dict:
        """Saves a new route (unapproved, version 1) and returns it with its new routeId."""
        if caller is not None:
            if not caller.is_caregiver:
                raise Forbidden("Only a caregiver can save a route.")
            if data.get("caregiverId") != caller.id:
                raise Forbidden("A route can only be saved as yourself: caregiverId must be your own id.")
        store = self._route_store()
        route = {**with_easy_read(data), "version": 1, "approved": False, "updatedAt": self._now_iso()}
        for _ in range(5):                       # ids are random; a clash is vanishingly rare
            candidate = self._ordered({**route, "routeId": "R-" + secrets.token_hex(3).upper()})
            try:
                store.create(candidate)
                return candidate
            except RouteExists:
                continue
        raise RuntimeError("could not allocate a route id")

    def get_route(self, route_id: str, caller: Caller | None = None) -> dict:
        route = self._route_store().get(route_id)
        if route is None or (caller is not None and not self._participates(caller, route)):
            raise RouteNotFound(route_id)
        return route

    def update_route(self, route_id: str, data: dict, expected_version: int, caller: Caller | None = None) -> dict:
        """Replaces the route's content. Raises RouteConflict if `expected_version` is out of date."""
        current = self.get_route(route_id, caller)
        if caller is not None and not self._owns(caller, current):
            raise Forbidden("Only the route's caregiver can change it.")
        for field in ("travellerId", "caregiverId"):
            if data.get(field) != current[field]:
                raise RouteInvalid(f"{field} can't be changed on a saved route")
        route = self._ordered({**with_easy_read(data), "routeId": route_id, "version": current["version"] + 1,
                               "approved": False, "updatedAt": self._now_iso()})
        self._route_store().replace(route, expected_version)
        return route

    def approve_route(self, route_id: str, caller: Caller | None = None) -> dict:
        """Marks the route approved (idempotent). The content and version are unchanged."""
        current = self.get_route(route_id, caller)
        if caller is not None and not self._owns(caller, current):
            raise Forbidden("Only the route's caregiver can approve it.")
        if current["approved"]:
            return current
        route = {**current, "approved": True, "updatedAt": self._now_iso()}
        self._route_store().replace(route, current["version"])
        return route

    def list_routes(self, traveller_id: str, caller: Caller | None = None) -> list[dict]:
        """A traveller's saved routes, most recently changed first. With a caller, only the ones that caller may
        see: asking for someone else's traveller gives an empty list, not an error."""
        routes = self._route_store().list_for_traveller(traveller_id)
        if caller is not None:
            routes = [r for r in routes if self._participates(caller, r)]
        return sorted(routes, key=lambda r: r["updatedAt"], reverse=True)

    @staticmethod
    def _ordered(route: dict) -> dict:
        """Same field order as contracts/examples/route.json, for readable output."""
        head = ["routeId", "travellerId", "caregiverId", "name", "version", "approved",
                "origin", "destination", "steps", "path", "updatedAt"]
        return {**{k: route[k] for k in head if k in route}, **{k: v for k, v in route.items() if k not in head}}

    # ===== trips (contracts/api.md: /api/trips) =====
    #
    # A trip is one journey on one day along an approved route. It pins the route's version, because a
    # disruption or an edit must never change a journey that is already under way. Statuses:
    # planned -> active -> completed. Starting a trip writes what the rest of the system reads
    # (contracts/redis-keys.md): the trip id in the set `active-trips` and its steps in `trip:{id}:route`.

    def _trip_store(self):
        if self._trips is None:
            raise RuntimeError("trips need a Redis client (or another trip store)")
        return self._trips

    def _redis(self):
        if self._r is None:
            raise RuntimeError("starting and ending trips needs a Redis client")
        return self._r

    @staticmethod
    def _public_trip(trip: dict) -> dict:
        return {k: v for k, v in trip.items() if k != "expiresAt"}      # expiresAt is only for DynamoDB's TTL

    def _stored_trip(self, trip_id: str, caller: Caller | None = None) -> dict:
        """The trip, or TripNotFound; also when `caller` may not see it (the route's caregiver or the trip's
        traveller may)."""
        trip = self._trip_store().get(trip_id)
        if trip is None or (caller is not None and not self._may_see_trip(caller, trip)):
            raise TripNotFound(trip_id)
        return trip

    def create_trip(self, route_id: str, trip_date: str, caller: Caller | None = None) -> dict:
        """A planned trip on an approved route. `trip_date` is "YYYY-MM-DD"."""
        route = self.get_route(route_id, caller)
        if caller is not None and not self._owns(caller, route):
            raise Forbidden("Only the route's caregiver can create a trip on it.")
        if not route["approved"]:
            raise TripConflict(f"Route {route_id} must be approved before a trip can be created.")
        # DynamoDB deletes the trip 30 days after its day (contracts/dynamo-db-tables.md).
        expires = datetime.strptime(trip_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=31)
        for _ in range(5):
            trip = {
                "tripId": "T-" + secrets.token_hex(3).upper(), "routeId": route_id, "routeVersion": route["version"],
                "travellerId": route["travellerId"], "date": trip_date, "status": "planned", "startedAt": None,
                "endedAt": None, "currentStepIndex": 0, "planVersion": 1, "override": None,
                "expiresAt": int(expires.timestamp()),
            }
            try:
                self._trip_store().create(trip)
                return self._public_trip(trip)
            except TripExists:
                continue
        raise RuntimeError("could not allocate a trip id")

    def get_trip(self, trip_id: str, caller: Caller | None = None) -> dict:
        return self._public_trip(self._stored_trip(trip_id, caller))

    def list_trips(self, traveller_id: str, trip_date: str | None = None, caller: Caller | None = None) -> list[dict]:
        """A traveller's trips, newest day first. With a caller, only the ones that caller may see."""
        trips = self._trip_store().list_for_traveller(traveller_id, trip_date)
        if caller is not None:
            routes: dict = {}                                  # one route lookup per route, not per trip
            trips = [t for t in trips if self._may_see_trip(caller, t, routes)]
        return [self._public_trip(t) for t in sorted(trips, key=lambda t: (t["date"], t["tripId"]), reverse=True)]

    def _route_for_trip(self, trip: dict) -> dict:
        """The route exactly as it was when the trip was created, or a conflict if it has since changed
        (only the latest version of a route is kept, so an edited route can't be used for the old trip)."""
        route = self.get_route(trip["routeId"])
        if route["version"] != trip["routeVersion"] or not route["approved"]:
            raise TripConflict(f"Route {trip['routeId']} was changed after this trip was created. "
                               "Approve the new version and create a new trip.")
        return route

    @staticmethod
    def _detector_step(step: dict) -> dict:
        """What the detectors need and nothing personal: ids, coordinates and public stop names. Instruction text
        and any names a caregiver typed stay out (contracts/dynamo-db-tables.md: detectors use IDs only)."""
        def point(p: dict) -> dict:
            return {"name": p.get("name") if p.get("code") else None, "code": p.get("code"),
                    "lat": p.get("lat"), "lng": p.get("lng")}
        cached = {"stepIndex": step["stepIndex"], "mode": step["mode"], "line": step.get("line"),
                  "from": point(step.get("from") or {}), "to": point(step.get("to") or {}),
                  "expectedDurationS": step.get("expectedDurationS"), "underground": step.get("underground")}
        if step.get("stops"):
            cached["stops"] = step["stops"]
        return cached

    def _publish_active(self, trip: dict, route: dict) -> None:
        """Idempotent, so a retry (or restarting an active trip after Redis lost its data) is safe."""
        cached = {"routeId": route["routeId"], "routeVersion": route["version"], "travellerId": route["travellerId"],
                  "steps": [self._detector_step(s) for s in route["steps"]], "path": route["path"]}
        pipe = self._redis().pipeline()
        pipe.set(f"trip:{trip['tripId']}:route", json.dumps(cached), ex=self._s.trip_key_ttl_s)
        pipe.sadd("active-trips", trip["tripId"])
        pipe.execute()

    def start_trip(self, trip_id: str, caller: Caller | None = None) -> dict:
        trip = self._stored_trip(trip_id, caller)
        if trip["status"] == "completed":
            raise TripConflict("This trip has already finished.")
        if trip["status"] == "active":                       # already started: just make sure Redis is right
            try:
                self._publish_active(trip, self._route_for_trip(trip))
            except TripConflict:
                pass
            return self._public_trip(trip)
        self._publish_active(trip, self._route_for_trip(trip))   # Redis first: if saving fails below, a retry finishes
        started = {**trip, "status": "active", "startedAt": self._now_iso()}
        self._trip_store().replace(started, "planned")
        return self._public_trip(started)

    def end_trip(self, trip_id: str, caller: Caller | None = None) -> dict:
        trip = self._stored_trip(trip_id, caller)
        if trip["status"] == "completed":
            return self._public_trip(trip)
        if trip["status"] == "planned":
            raise TripConflict("This trip hasn't started yet.")
        pipe = self._redis().pipeline()
        pipe.srem("active-trips", trip_id)
        pipe.delete(f"trip:{trip_id}:route")
        pipe.execute()
        ended = {**trip, "status": "completed", "endedAt": self._now_iso()}
        self._trip_store().replace(ended, "active")
        return self._public_trip(ended)

    # ===== route pack: the offline file for the traveller's phone (contracts/examples/route-pack.json) =====

    def build_pack(self, trip_id: str, caller: Caller | None = None) -> dict:
        """The pack for a trip's route. Names that live in the Profiles service (traveller, caregiver) are null
        until that service exists; the app must cope with null."""
        route = self._route_for_trip(self._stored_trip(trip_id, caller))
        steps = []
        for step in route["steps"]:
            target = step.get("to") or {}
            steps.append({
                "stepIndex": step["stepIndex"], "mode": step["mode"],
                "easyRead": step.get("easyRead") or easy_read(step),
                "lat": target.get("lat"), "lng": target.get("lng"),     # where this step ends
                "underground": step.get("underground"),
                "photoUrl": step.get("photoKey"), "audioUrl": step.get("audioKey"),
                "backup": (step.get("backup") or {}).get("instruction"),
            })
        return {
            "packVersion": f"{route['routeId']}-v{route['version']}", "routeId": route["routeId"],
            "routeVersion": route["version"], "travellerName": None, "steps": steps, "path": route["path"],
            "caregiver": {"name": None, "phone": None}, "messages": dict(PACK_MESSAGES),
            "generatedAt": self._now_iso(),
        }

    def trip_pack(self, trip_id: str, caller: Caller | None = None) -> dict:
        """Builds the pack, stores it in S3 and returns a temporary link: {"packUrl", "packVersion"}."""
        pack = self.build_pack(trip_id, caller)              # problems with the trip itself are reported first
        if self._packs is None:
            raise PackUnavailable("pack storage is not configured")
        key = f"packs/{pack['routeId']}/v{pack['routeVersion']}.json"
        self._packs.put(key, json.dumps(pack).encode("utf-8"))
        return {"packUrl": self._packs.url(key), "packVersion": pack["packVersion"]}
