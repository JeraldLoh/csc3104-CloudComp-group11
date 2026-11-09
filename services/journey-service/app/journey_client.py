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

from . import bridge, easy_read_ai, landmarks, streetview
from .auth import Caller, Forbidden
from .cache import JsonCache
from .config import Settings
from .easy_read import LINE_NAMES, easy_read, route_wording, with_easy_read
from .geo import EARTH_RADIUS_M, distance_m, distance_to_path_m, path_between
from .lta import LINE_CODES, SST, LtaClient, LtaError
from .onemap_token import OneMapToken
from .pack_store import PackUnavailable
from .route_store import RedisRouteStore, RouteExists, RouteInvalid, RouteNotFound
from .trip_store import RedisTripStore, TripConflict, TripExists, TripNotFound

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

# Standard texts for the traveller's phone
PACK_MESSAGES = {
    "somethingChanged": "Something has changed. Stay where you are. Your caregiver has been told.",
    "offRoute": "You may be going the wrong way. Stop and look at the next picture.",
    "help": "Press the big red button if you need help.",
}


# Exceptions ---------------------------------------------------------------------------
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


# Helpers ---------------------------------------------------------------------------
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


# Client ------------------------------------------------------------------------

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
        notify: Callable[[dict], None] | None = None,  # tells the caregiver (careroute-disruptions, see trip changes)
        photo_http: httpx.Client | None = None,      # Google Street View
        speech=None,                                 # speech.Speech: spoken instructions in packs; None = none
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
        self._notify = notify
        self._photo_http = photo_http or httpx.Client()
        self._speech = speech

    # OneMap transport =====

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

    # LTA DataMall (the work is done by self.lta, see lta.py) =====

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

    # OneMap ---------------

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

    STATION_EXITS_TTL_S = 7 * 24 * 3600               # exits don't move, so cache them for a week. Cached to ensure the API can still return them when OneMap is down.
    NO_EXITS_TTL_S = 3600                             # if a station has no exits, cache that for an hour (OneMap may be wrong or incomplete)

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

    # Buildings ---------------------------------------------------------------------------

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
        # A breakdown: each option says where it rides with no trains (`affected`) and the free bus past it
        # (`bridges`, for the caregiver to see, never saved). Those go last; if it hits them all, buses only too.
        alerts = live.alerts()
        broken = alerts["segments"] if alerts["status"] >= self._s.disruption_min_status else []
        for opt in options:
            opt["affected"] = self._affected(opt["steps"], opt["stepPaths"], broken)
            opt["bridges"] = self._bridges(opt["steps"], broken) if opt["affected"] else []
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

    # Saved routes and trips
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
        # Read the steps aloud now, once (speech.py): only approved routes get trips, and an approved route's
        # wording is final, so its packs find the audio ready instead of waiting for Polly.
        self._speak(current["steps"])
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

    # Step photos and landmarks and their Street View photos
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

    # Trips 
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

    def _public_trip(self, trip: dict) -> dict:
        # expiresAt is only for DynamoDB's TTL; planSteps (the whole changed plan) is in the pack, `override` says what
        # changed (its steps with a temporary photoUrl each, as a route's).
        out = {k: v for k, v in trip.items() if k not in ("expiresAt", "planSteps", "planPaths")}
        if out.get("override"):
            out["override"] = {**out["override"], "steps": [self._step_with_urls(s) for s in out["override"]["steps"]]}
        return out

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

    @staticmethod
    def _plan_for_trip(trip: dict, route: dict) -> list[dict]:
        """The steps the traveller follows: the route's, or the plan a bridging bus or re-route changed them to."""
        return trip.get("planSteps") or route["steps"]

    def _publish_active(self, trip: dict, route: dict) -> None:
        """Idempotent, so a retry (or restarting an active trip after Redis lost its data) is safe. The trip's own
        plan, so the detectors and the disruption checker follow a bridging bus or a re-route."""
        cached = {"routeId": route["routeId"], "routeVersion": route["version"], "travellerId": route["travellerId"],
                  "steps": [self._detector_step(s) for s in self._plan_for_trip(trip, route)], "path": route["path"]}
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
            "steps": self._with_photos({"steps": [{**s, "easyRead": t} for s, t in
                                                  zip(made["steps"], self._wording(plan, index, index, made["steps"], route))]})["steps"],
            "createdAt": self._iso(now), "sendAt": self._iso(now + timedelta(seconds=self._s.bridge_auto_send_s)),
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
            made = bridge.bridge_for(plan[index], segment, self._free_bus, self._station_point)
            if made:
                return f"{segment.get('line')}:{','.join(segment['stations'])}", index, segment, made
        return None

    FREE_BUS_STOP_M = 300                # a bus stop this close to a station counts as at the station

    def _free_bus(self, leave: dict, target: dict) -> dict | None:
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
        sent = {**offer, "status": "sent", "sentAt": self._iso(now), "sentBy": by}
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
            "message": message, "detectedAt": self._iso(self._clock()),
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
        found = self._bridge_for_trip(trip_id, plan, self._broken())
        if found is None:
            raise TripConflict("There is no free bus past a breakdown on this trip's steps. Plan a new route instead.")
        offer = self._bridge_proposal(found, plan, route, self._clock())
        return {"kind": offer["bridgeKind"], "busNo": offer["busNo"], "leaveAt": offer["leaveAt"], "target": offer["target"],
                "steps": [self._step_with_urls(s) for s in offer["steps"]]}

    def use_bridge(self, trip_id: str, caller: Caller | None = None) -> dict:
        """The caregiver switches the trip to the free bus past a breakdown now (e.g. before it starts), instead of
        waiting for the watcher to propose it."""
        trip, route, plan = self._owned_active_trip(trip_id, caller, planned_too=True)
        found = self._bridge_for_trip(trip_id, plan, self._broken())
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
                              "at": datetime.fromisoformat(c["timestamp"].replace("Z", "+00:00"))})
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

    def _broken(self) -> list[dict]:
        """LTA's broken stretches now (TrainServiceAlerts segments); none if LTA can't be asked."""
        try:
            alerts = self.lta.train_alerts()
        except LtaError as e:
            log.warning("no train alerts (%s)", type(e).__name__)
            return []
        return alerts["segments"] if alerts["status"] >= self._s.disruption_min_status else []

    def _stop_point(self, stop: dict) -> tuple[float, float] | None:
        return (stop["lat"], stop["lng"]) if stop.get("lat") is not None else self._station_point(stop)

    def _affected(self, steps: list[dict], paths: list, segments: list[dict]) -> list[dict]:
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
                ends = [self._stop_point(first), self._stop_point(last)]
                found.append({"stepIndex": i, "line": segment.get("line"), "from": first.get("name"),
                              "to": last.get("name"), "path": path_between(line, *ends) if all(ends) and line else line,
                              "message": f"No trains between {first.get('name')} and {last.get('name')}"})
        return found

    def _bridges(self, steps: list[dict], segments: list[dict]) -> list[dict]:
        """The free bus past each broken stretch `steps` ride through (bridge.bridge_for), to show with a plan."""
        found = []
        for segment in segments:
            for i, step in enumerate(steps):
                if bridge.through(step, set(segment["stations"])) is None:
                    continue
                try:
                    made = bridge.bridge_for(step, segment, self._free_bus, self._station_point)
                except LtaError as e:
                    log.warning("plan: no free buses (%s)", type(e).__name__)
                    made = None
                if made:
                    ride = next(s for s in made["steps"] if s.get("bridge"))
                    found.append({"stepIndex": i, "kind": made["kind"], "busNo": ride["bridge"].get("serviceNo"),
                                  "leaveAt": made["leaveAt"].get("name"), "target": made["target"].get("name"),
                                  "steps": [{"mode": s["mode"], "instruction": s["instruction"]} for s in made["steps"]]})
        return found

    def trip_plan(self, trip_id: str, caller: Caller | None = None) -> dict:
        """The steps the traveller of a trip under way follows now (with a bridging bus or re-route sent), each with
        its line on the map, and `affected`: where those steps ride with no trains running (LTA's broken stretches),
        as lines to draw over them."""
        trip = self._stored_trip(trip_id, caller)
        if trip["status"] != "active":
            raise TripConflict("Only a trip that is under way has a plan to follow.")
        route, plan = self._active_plan(trip)
        paths = self._plan_paths(trip, route, plan)
        affected = self._affected(plan, paths, self._broken())
        steps = [{k: v for k, v in self._step_with_urls(s).items() if k not in ("photoKey", "audioKey", "backup")} for s in plan]
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
                "createdAt": self._iso(self._clock()), "sendAt": None, "sentAt": None, "sentBy": None}

    @staticmethod
    def _iso(when: datetime) -> str:
        return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    @staticmethod
    def _clock_time(iso: str) -> str:
        return datetime.fromisoformat(iso).astimezone(SST).strftime("%H:%M")

    # ===== route pack: the offline file for the traveller's phone (contracts/examples/route-pack.json) =====

    def build_pack(self, trip_id: str, caller: Caller | None = None) -> dict:
        """The pack for a trip's route. Names that live in the Profiles service (traveller, caregiver) are null
        until that service exists; the app must cope with null."""
        trip = self._stored_trip(trip_id, caller)
        route = self._route_for_trip(trip)
        plan_steps = self._plan_for_trip(trip, route)
        wording = [self._pack_wording(step) for step in plan_steps]
        audio = self._audio_keys(plan_steps, wording)
        steps = []
        for step, words, audio_key in zip(plan_steps, wording, audio):
            target = step.get("to") or {}
            steps.append({
                "stepIndex": step["stepIndex"], "mode": step["mode"],
                "easyRead": words,
                "lat": target.get("lat"), "lng": target.get("lng"),     # where this step ends
                "underground": step.get("underground"),
                # audioUrl: the step read aloud (speech.py), a temporary link like the photo's; null = the phone
                # reads it with its own voice
                "photoUrl": self._photo_url(step.get("photoKey")), "audioUrl": self._photo_url(audio_key),
                "backup": (step.get("backup") or {}).get("instruction"),
                # What the wording tells them to look out for, with a photo of each. `sameView`: one with no photo of
                # its own says which photo shows it ("step", or another landmark's name), so the phone can say so.
                "landmarks": [{"name": m["name"], "photoUrl": self._photo_url(m.get("photoKey")),
                               "sameView": m.get("sameView")}
                              for m in step.get("landmarks") or []],
            })
        # A changed plan is a new pack version, so the phone (which polls for one) shows "Your trip has changed".
        plan = trip.get("planVersion") or 1
        return {
            "packVersion": f"{route['routeId']}-v{route['version']}" + (f"-b{plan}" if plan > 1 else ""),
            "routeId": route["routeId"],
            "routeVersion": route["version"], "travellerName": None, "steps": steps, "path": route["path"],
            "caregiver": {"name": None, "phone": None}, "messages": dict(PACK_MESSAGES),
            "generatedAt": self._now_iso(),
        }

    @staticmethod
    def _pack_wording(step: dict) -> str:
        """The words the pack shows (and reads aloud) for a step. One place, so audio made early (_speak) has the
        same key the pack looks for."""
        return step.get("easyRead") or easy_read(step)

    def _speak(self, steps: list[dict]) -> None:
        """Makes the steps' spoken audio now, at a single caregiver action (approving a route, sending a changed plan),
        so building a pack doesn't call Polly and two pods building the same new pack don't both make it.
        Best-effort: what fails here is made when the pack is built."""
        self._audio_keys(steps, [self._pack_wording(s) for s in steps])

    def _audio_keys(self, plan: list[dict], wording: list[str]) -> list[str | None]:
        """Each step's audio in the pack bucket: the step's own `audioKey` if it has one, else its wording read
        aloud (made now if it doesn't exist yet; usually it was, by _speak). None with TTS_MODE=off or when Polly
        fails."""
        own = [step.get("audioKey") for step in plan]
        if self._speech is None or self._packs is None:
            return own
        spoken = self._speech.keys([None if key else words for key, words in zip(own, wording)])
        return [key or said for key, said in zip(own, spoken)]

    def trip_pack(self, trip_id: str, caller: Caller | None = None) -> dict:
        """Builds the pack, stores it in S3 and returns a temporary link: {"packUrl", "packVersion"}."""
        pack = self.build_pack(trip_id, caller)              # problems with the trip itself are reported first
        if self._packs is None:
            raise PackUnavailable("pack storage is not configured")
        key = f"packs/{pack['routeId']}/{pack['packVersion'].split('-', 2)[-1]}.json"      # v3.json, v3-b2.json
        self._packs.put(key, json.dumps(pack).encode("utf-8"))
        return {"packUrl": self._packs.url(key), "packVersion": pack["packVersion"]}
