"""JourneyClient: the API's logic in one class (proof of concept): planning, saved routes, trips and packs.

    client = JourneyClient(settings, redis_client=redis)

    client.search_places("Tampines Mall")        OneMap place/address search
    client.plan(origin, destination, ...)        OneMap itineraries -> contract steps, checked against LTA
    client.create_route / create_trip / ...      saved routes (with Street View photos), trips, route packs
    client.suggest_easy_read / upload_photo      AI wording for the caregiver to review; their own step photo
    client.bus_arrival("83139")                  LTA data, through the shared LtaClient in client.lta

The LTA client lives in lta.py and the disruption checker in disruption.py: they are shared with / run by the
Lambda, which must not need this file's login, route and pack code.

Failures raise LtaError / OneMapError subclasses; plan() degrades instead when only LTA is down.
Redis is optional here (a cache for lookups; stores for routes and trips when no DynamoDB table is configured).
"""
import hashlib
import json
import logging
import math
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import BinaryIO, Callable

import httpx

from . import easy_read_ai, landmarks, streetview
from .auth import Caller, Forbidden
from .cache import JsonCache
from .config import Settings
from .easy_read import LINE_NAMES, easy_read, route_wording, with_easy_read
from .geo import EARTH_RADIUS_M, distance_to_path_m
from .lta import LINE_CODES, SST, LtaClient, LtaError
from .onemap_token import OneMapToken
from .pack_store import PackUnavailable
from .route_store import RedisRouteStore, RouteExists, RouteInvalid, RouteNotFound
from .trip_store import RedisTripStore, TripConflict, TripExists, TripNotFound

log = logging.getLogger("journey")


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


class PhotoInvalid(ValueError):
    """An uploaded step photo that isn't a JPEG, PNG or WebP image, or is too big."""


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

    def __init__(self, client: "JourneyClient"):
        self._c = client
        self._lta = client._plan_lta                 # no retries: live data is a bonus
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
        mode=None,                                   # data_mode.DataMode; None = ONEMAP_MODE / LTA_MODE, fixed
        easyread_http: httpx.Client | None = None,   # the Easy-read Service (tests pass a mocked one)
        photo_http: httpx.Client | None = None,      # Google Street View
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
        # Live or saved data (data_mode.py): read on every call, so a switch takes effect without a restart.
        self._mode = mode
        # LTA data comes from the shared client; the API calls it through the methods below.
        self.lta = lta or LtaClient(settings, redis_client=redis_client, sleep=sleep, clock=clock, http=lta_http,
                                    mode=mode)
        self._plan_lta = self.lta.without_retries()          # live data in a plan is a bonus: never retried
        self._onemap_fixed_live = onemap_http is not None    # given (tests): always used, the data mode is ignored
        self._onemap_http = onemap_http or httpx.Client(
            base_url=settings.onemap_base_url, timeout=settings.onemap_timeout_s)
        self._onemap_recorded_http: httpx.Client | None = None    # built on first use in saved-data mode
        # Logs in with the OneMap account when there is one, and only when live data is asked for.
        self._token = OneMapToken(settings, redis_client, clock=clock)
        self._easyread_http = easyread_http or httpx.Client()
        self._photo_http = photo_http or httpx.Client()

    # ===== OneMap transport =====

    @property
    def _recorded(self) -> bool:
        if self._onemap_fixed_live:
            return False
        return (self._mode.get("onemap") if self._mode else self._s.onemap_mode) == "recorded"

    def _onemap_client(self, recorded: bool) -> httpx.Client:
        if not recorded:
            return self._onemap_http
        if self._onemap_recorded_http is None:
            from .replay import RecordedOneMap       # only loaded in saved-data mode (see replay.py)
            self._onemap_recorded_http = httpx.Client(base_url=self._s.onemap_base_url, transport=RecordedOneMap())
        return self._onemap_recorded_http

    def _onemap_get(self, path: str, params: dict, auth: bool = True) -> dict:
        recorded = self._recorded                   # read once, so one request never mixes modes
        http = self._onemap_client(recorded)
        headers = {}
        if auth and not recorded:
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
                r = http.get(path, params=params, headers=headers)
            except httpx.HTTPError as e:
                last = e
                continue
            if r.status_code in (401, 403) and auth and not recorded and self._token.enabled and not renewed:
                renewed = True                       # it expired (or was revoked) early: log in again, once
                fresh = self._token.get(refresh=True)
                if fresh:
                    headers["Authorization"] = f"Bearer {fresh}"
                    try:
                        r = http.get(path, params=params, headers=headers)
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

    STATION_EXITS_TTL_S = 7 * 24 * 3600               # exits don't move
    NO_EXITS_TTL_S = 3600                             # none found: maybe a blip in OneMap's search, so ask again soon

    def station_exits(self, station_name: str) -> list[tuple[float, float]]:
        """Where an MRT/LRT station's exits are, from OneMap's search ("SENGKANG MRT STATION EXIT A"); [] if unknown.
        Exits are fixed public data and the search needs no token, so this always asks live OneMap, also in
        saved-data mode (like the Street View photos it is for). Best-effort: a failure means no exits."""
        name = re.sub(r"\s*\(.*?\)", "", station_name).strip().upper()     # "Sengkang MRT Station (NE16)"

        def look_up() -> list[list[float]]:
            exits = []
            for page in (1, 2, 3):                       # 10 results a page; big interchanges have more exits
                r = self._onemap_http.get("/api/common/elastic/search", params={
                    "searchVal": f"{name} EXIT", "returnGeom": "Y", "getAddrDetails": "N", "pageNum": str(page)})
                r.raise_for_status()
                found = r.json()
                exits += [[float(x["LATITUDE"]), float(x["LONGITUDE"])] for x in found.get("results") or []
                          if (x.get("SEARCHVAL") or "").upper().startswith(f"{name} EXIT ")]
                if page >= int(found.get("totalNumPages") or 1):
                    break
            return exits

        try:
            return [(lat, lng) for lat, lng in self._cache.cached(f"station-exits:{name}", self.STATION_EXITS_TTL_S,
                                                                  look_up, empty_ttl_s=self.NO_EXITS_TTL_S)]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.warning("couldn't look up the exits of %s (%s)", name, type(e).__name__)
            return []

    BUILDINGS_TTL_S = 30 * 24 * 3600                  # buildings don't move
    BUILDINGS_RADIUS_M = 100

    def nearby_buildings(self, lat: float, lng: float) -> list[dict]:
        """Named buildings within BUILDINGS_RADIUS_M of a point, [{name, lat, lng}], from OneMap's reverse geocode
        (for landmarks.py). Like station_exits: fixed public data, so always live OneMap, also in saved-data mode,
        and best-effort: no token or a failure means none. Cached on a ~10 m grid."""
        def look_up() -> list[dict]:
            token = self._token.get() or self._s.onemap_api_key
            if not token:
                return []
            r = self._onemap_http.get("/api/public/revgeocode", headers={"Authorization": f"Bearer {token}"}, params={
                "location": f"{lat},{lng}", "buffer": str(self.BUILDINGS_RADIUS_M), "addressType": "All",
                "otherFeatures": "N"})
            r.raise_for_status()
            out = []
            for b in r.json().get("GeocodeInfo") or []:
                name = _clean(re.sub(r"\s*\(.*?\)", "", b.get("BUILDINGNAME") or ""))   # "HAWKER CENTRE (BLK 208B ...)"
                if name:
                    out.append({"name": titlecase(name), "lat": float(b["LATITUDE"]), "lng": float(b["LONGITUDE"])})
            return out

        try:
            return self._cache.cached(f"buildings:{lat:.4f}_{lng:.4f}", self.BUILDINGS_TTL_S, look_up,
                                      empty_ttl_s=self.NO_EXITS_TTL_S)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.warning("couldn't look up buildings near %.4f,%.4f (%s)", lat, lng, type(e).__name__)
            return []

    def route_landmarks(self, route: dict) -> dict[int, dict[str, list[dict]]]:
        """What the traveller will see at each spot of the route (landmarks.py), the lookups run at once."""
        found = landmarks.spots(route)
        points = sorted({(s.lat, s.lng) for by_spot in found.values() for s in by_spot.values()})
        if not points:
            return {}
        with ThreadPoolExecutor(max_workers=min(8, len(points))) as pool:
            buildings = dict(zip(points, pool.map(lambda p: self.nearby_buildings(*p), points)))
        return landmarks.offer(route, found, lambda lat, lng: buildings[(lat, lng)])

    def route_pt(self, start: tuple[float, float], end: tuple[float, float], when: datetime,
                 mode: str = "TRANSIT", max_walk_m: int | None = None, num_itineraries: int | None = None) -> dict:
        """OneMap's raw public-transport itineraries: plan.itineraries[].legs[] (see app/saved_data/fixtures)."""
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
        live.prefetch([lookup for it in itineraries for leg in it.get("legs", []) for lookup in self._lookups(leg)])
        walk_limit = max_walk_m or self._s.plan_max_walk_m
        options = [self._option(n, it, live, walk_limit) for n, it in enumerate(itineraries, start=1)]
        if live.degraded:
            for opt in options:
                opt["warnings"].append(dict(LIVE_DATA_UNAVAILABLE))
        options = sorted(options, key=SORTS[sort_by])           # stable: ties keep OneMap's order
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
        return {"warnings": warnings, "checkedAt": self._now_iso()}

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
        if step_mode in ("bus", "train"):
            # Every stop on the leg: for a train, so check_disruptions() can tell whether an MRT fault falls between
            # the two ends and easy_read() which way it goes; for a bus, the stop before getting off ("After
            # Block 301, press the bell").
            every = [leg["from"], *(leg.get("intermediateStops") or []), leg["to"]]
            step["stops"] = [{"code": s.get("stopCode"), "name": pretty(s.get("name"))} for s in every]
        step["easyRead"] = easy_read(step)
        return step

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
        route = {**self._with_photos(with_easy_read(data)), "version": 1, "approved": False,
                 "updatedAt": self._now_iso()}
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
        route = self._ordered({**self._with_photos(with_easy_read(data)), "routeId": route_id,
                               "version": current["version"] + 1, "approved": False, "updatedAt": self._now_iso()})
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

    def delete_route(self, route_id: str, expected_version: int | None = None, caller: Caller | None = None) -> None:
        """Removes a saved route. Raises RouteConflict if `expected_version` is given and out of date, and
        TripConflict while one of its trips is under way. Its planned trips stay but can no longer start."""
        current = self.get_route(route_id, caller)
        if caller is not None and not self._owns(caller, current):
            raise Forbidden("Only the route's caregiver can delete it.")
        if self._trips is not None and any(
                t["routeId"] == route_id and t["status"] == "active"
                for t in self._trips.list_for_traveller(current["travellerId"])):
            raise TripConflict(f"Route {route_id} has a trip under way. End that trip before deleting the route.")
        self._route_store().delete(route_id, expected_version)

    def list_routes(self, traveller_id: str, caller: Caller | None = None) -> list[dict]:
        """A traveller's saved routes, most recently changed first. With a caller, only the ones that caller may
        see: asking for someone else's traveller gives an empty list, not an error."""
        routes = self._route_store().list_for_traveller(traveller_id)
        if caller is not None:
            routes = [r for r in routes if self._participates(caller, r)]
        return sorted(routes, key=lambda r: r["updatedAt"], reverse=True)

    # ----- step photos and AI wording -----
    #
    # A step's `photoKey` is an S3 key in the pack bucket: a Street View photo (media/streetview/..., filled in on
    # save for steps without one) or the caregiver's upload (media/uploads/..., kept like caregiver-written
    # easyRead). Responses carry `photoUrl`, a presigned link made per response and never stored.
    #
    # A step's `landmarks` (from "Simplify with AI", landmarks.py) are the buildings its wording tells the traveller
    # to look out for, each with its own Street View photo, taken from the street facing it. They are kept only
    # while the wording still names them, so a landmark the caregiver edits out of the text goes with it.

    def _with_photos(self, data: dict) -> dict:
        """Street View photos for steps with no photoKey (under rough/ when only an old panorama was there), and a
        recent one for a step with a rough photo once Google has one; and for landmarks without one. Best-effort:
        a step stays without one when Street View has no panorama there at all, Google or S3 fails, or photos are
        off (no API key or no bucket)."""
        steps = [self._stored_step(s) for s in data["steps"]]
        if not self._photos_on:
            return {**data, "steps": steps}
        wanted: dict[tuple[int, int | None], tuple] = {}         # (step, landmark or None) -> (point, seen from)
        for i, step in enumerate(steps):
            key = step.get("photoKey")
            arrives = data.get("destination") if i == len(steps) - 1 else None
            point = self._photo_target(step, arrives) if not key or streetview.is_rough(key) else None
            if point:
                wanted[(i, None)] = (point, None)
            for n, mark in enumerate(step.get("landmarks") or []):
                if not mark.get("photoKey") and not mark.get("sameView"):        # sameView: shown in another photo
                    wanted[(i, n)] = self._landmark_view(mark)
        for (i, n), key in self._street_photos(wanted).items():
            if n is None:
                steps[i] = {**steps[i], "photoKey": key}
            else:
                steps[i]["landmarks"][n] = {**steps[i]["landmarks"][n], "photoKey": key}
        return {**data, "steps": steps}

    @staticmethod
    def _stored_step(step: dict) -> dict:
        """A step as it is saved: no presigned links, and only the landmarks its wording still names."""
        step = {k: v for k, v in step.items() if k != "photoUrl"}
        if "landmarks" in step:
            text = (step.get("easyRead") or "").lower()
            kept = [{k: v for k, v in m.items() if k != "photoUrl"} for m in step["landmarks"] or []
                    if m.get("name") and m["name"].lower() in text]
            step["landmarks"] = kept or None
        return step

    @property
    def _photos_on(self) -> bool:
        return bool(self._s.google_maps_api_key and self._packs)

    @staticmethod
    def _landmark_view(mark: dict) -> tuple:
        """(the building, the traveller's spot it is photographed from, if known)."""
        seen = mark.get("seenFrom")
        return (mark["lat"], mark["lng"]), (tuple(seen) if seen else None)

    def _street_photos(self, wanted: dict) -> dict:
        """{id: S3 key} of a Street View photo looking at each {id: (point, seen from or None)}, fetched at once;
        ids with none are left out. The same view is fetched once, here and across routes (see photo_key)."""
        if not wanted:
            return {}
        with ThreadPoolExecutor(max_workers=min(8, len(wanted))) as pool:
            keys = dict(zip(wanted, pool.map(lambda view: self._street_photo(*view), wanted.values())))
        return {i: key for i, key in keys.items() if key}

    def _street_photo(self, point: tuple[float, float], near: tuple[float, float] | None = None) -> str | None:
        good, rough = streetview.photo_key(*point, near=near), streetview.photo_key(*point, rough=True, near=near)
        try:
            if self._packs.exists(good):          # the same place in another route: already fetched
                return good
            # A rough photo is looked for again on every save, so a recent one replaces it once Google has one.
            have_rough = self._packs.exists(rough)
            photo = streetview.fetch(*point, self._photo_http, self._s.google_maps_api_key,
                                     self._s.streetview_timeout_s, self._s.streetview_max_age_years, have_rough,
                                     near)
            if photo is None:                     # Google down or refusing: the rough photo is still better
                return rough if have_rough else None
            key = rough if photo.rough else good
            if photo.image is not None:
                self._packs.put(key, photo.image, "image/jpeg")
            return key
        except PackUnavailable as e:
            log.warning("couldn't store a street view photo: %s", e)
            return None

    STATION_NAME = re.compile(r"\b(MRT|LRT) Station\b", re.IGNORECASE)

    def _photo_target(self, step: dict, destination: dict | None = None) -> tuple[float, float] | None:
        """Where a step's photo looks: the end of the step, but for a walk to a station the exit nearest to where
        the walk starts (the station's own point is underground or inside a mall).

        `destination`: the route's, given for its last step. A last walk ends there, so the photo is of the
        destination itself, not of where OneMap's leg ends (up to ~50 m off: the saved demo trips were recorded
        with rounded endpoints). A destination that is a station ("Orchard MRT Station (NS22)") gets its exit."""
        start = step.get("from") or {}
        station = streetview.walks_to_station(step)
        end = step.get("to") or {}
        if (destination and destination.get("lat") is not None and destination.get("lng") is not None
                and step.get("mode") == "walk" and not end.get("code")):
            if not self.STATION_NAME.search(destination.get("name") or ""):
                return destination["lat"], destination["lng"]
            station = destination
        if station and start.get("lat") is not None and start.get("lng") is not None:
            exit_point = streetview.nearest(self.station_exits(station["name"]), (start["lat"], start["lng"]))
            if exit_point:
                return exit_point
            if station is destination:
                return destination["lat"], destination["lng"]
        return streetview.target(step)

    def _photo_url(self, key: str | None) -> str | None:
        return self._packs.url(key, self._s.photo_url_ttl_s) if key and self._packs else None

    def with_photo_urls(self, route: dict) -> dict:
        """The route as the API returns it: each step, and each of its landmarks, with a temporary `photoUrl` for
        its photo (or null)."""
        return {**route, "steps": [self._step_with_urls(s) for s in route["steps"]]}

    def _step_with_urls(self, step: dict) -> dict:
        out = {**step, "photoUrl": self._photo_url(step.get("photoKey"))}
        if step.get("landmarks"):
            out["landmarks"] = [{**m, "photoUrl": self._photo_url(m.get("photoKey"))} for m in step["landmarks"]]
        return out

    # Magic bytes, so the file's own claim about its type isn't trusted. WebP is RIFF....WEBP, checked below.
    PHOTO_TYPES = ((b"\xff\xd8\xff", "jpg", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "png", "image/png"))

    def upload_photo(self, route_id: str, photo: bytes | BinaryIO, caller: Caller | None = None) -> dict:
        """Stores a caregiver's photo for a step: {"photoKey", "photoUrl"}. The route itself is not changed: the
        editor puts the key in the step and the caregiver's save stores it (so Cancel really discards it).
        A file is only read once the route and the caller are checked, and only one byte past the size limit."""
        route = self.get_route(route_id, caller)
        if caller is not None and not self._owns(caller, route):
            raise Forbidden("Only the route's caregiver can change its photos.")
        data = photo if isinstance(photo, bytes) else photo.read(self._s.photo_max_bytes + 1)
        if len(data) > self._s.photo_max_bytes:
            raise PhotoInvalid(f"The photo is too big. The limit is {self._s.photo_max_bytes // (1024 * 1024)} MB.")
        found = next(((ext, ctype) for magic, ext, ctype in self.PHOTO_TYPES if data.startswith(magic)), None)
        if found is None and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            found = ("webp", "image/webp")
        if found is None:
            raise PhotoInvalid("The file isn't a photo. Use a JPEG, PNG or WebP image.")
        if self._packs is None:
            raise PackUnavailable("photo storage is not configured")
        key = f"media/uploads/{hashlib.sha256(data).hexdigest()[:16]}.{found[0]}"
        self._packs.put(key, data, found[1])
        return {"photoKey": key, "photoUrl": self._photo_url(key)}

    def suggest_easy_read(self, route_id: str, caller: Caller | None = None) -> dict:
        """AI wording for every step of a saved route, for the caregiver to check and edit before saving. Nothing
        is saved. Template wording (provider "template") when the Easy-read Service can't help (easy_read_ai.py)."""
        route = self.get_route(route_id, caller)
        if caller is not None and not self._owns(caller, route):
            raise Forbidden("Only the route's caregiver can reword it.")
        found = self.route_landmarks(route) if self._s.easyread_url and self._s.easyread_landmarks else {}
        answer = easy_read_ai.suggest(route["steps"], self._easyread_http, self._s.easyread_url,
                                      self._s.easyread_timeout_s, found, (route.get("destination") or {}).get("name"))
        # A photo of each landmark the wording names, so the caregiver sees what the traveller will look for
        # before saving (each new one is a paid Street View image; the same place is fetched only once).
        if self._photos_on:
            self._photograph_landmarks(route, answer["steps"])
        for s in answer["steps"]:
            s["landmarks"] = [{**m, "photoKey": m.get("photoKey"), "sameView": m.get("sameView"),
                               "photoUrl": self._photo_url(m.get("photoKey"))} for m in s["landmarks"]]
        return answer

    def _photograph_landmarks(self, route: dict, steps: list[dict]) -> None:
        """A photo of each landmark in `steps` (the AI's answer), unless it would look (nearly) the same as the
        step's own photo or an earlier landmark's, e.g. a mall next to the bus stop, seen from the stop. Such a
        landmark gets no photo of its own; `sameView` says where it is seen instead ("step", or the other
        landmark's name). Which panorama and direction each photo would use is free to find out (metadata)."""
        by_index = {s["stepIndex"]: s for s in route["steps"]}
        last = route["steps"][-1]["stepIndex"] if route["steps"] else None
        views: dict[tuple[int, int | None], tuple] = {}            # (step, landmark or None) -> (point, seen from)
        for i, s in enumerate(steps):
            step = by_index.get(s["stepIndex"])
            if not s["landmarks"] or step is None:
                continue
            key = step.get("photoKey")
            target = None if key and not key.startswith("media/streetview/") else self._photo_target(
                step, route.get("destination") if s["stepIndex"] == last else None)     # an upload can't be compared
            if target:
                views[(i, None)] = (target, None)
            for n, m in enumerate(s["landmarks"]):
                views[(i, n)] = self._landmark_view(m)
        with ThreadPoolExecutor(max_workers=min(8, len(views) or 1)) as pool:
            seen_as = dict(zip(views, pool.map(lambda v: streetview.view(
                *v[0], self._photo_http, self._s.google_maps_api_key, self._s.streetview_timeout_s,
                self._s.streetview_max_age_years, near=v[1]), views.values())))
        wanted = {}
        for i, s in enumerate(steps):
            shown = [("step", seen_as[(i, None)])] if seen_as.get((i, None)) else []
            for n, m in enumerate(s["landmarks"]):
                mine = seen_as.get((i, n))
                same = next((label for label, other in shown if mine and streetview.same_view(mine, other)), None)
                if same:
                    m["sameView"] = same
                    continue
                if mine:
                    shown.append((m["name"], mine))
                wanted[(i, n)] = views[(i, n)]
        for (i, n), key in self._street_photos(wanted).items():
            steps[i]["landmarks"][n]["photoKey"] = key

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
        try:
            route = self.get_route(trip["routeId"])
        except RouteNotFound:
            raise TripConflict(f"Route {trip['routeId']} was deleted after this trip was created. "
                               "Create a new trip on another route.") from None
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
                "photoUrl": self._photo_url(step.get("photoKey")), "audioUrl": step.get("audioKey"),
                "backup": (step.get("backup") or {}).get("instruction"),
                # What the wording tells them to look out for, with a photo of each.
                "landmarks": [{"name": m["name"], "photoUrl": self._photo_url(m.get("photoKey"))}
                              for m in step.get("landmarks") or []],
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
