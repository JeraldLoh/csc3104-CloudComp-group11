"""LTA DataMall client (API User Guide v6.10), shared by the API container and the disruption Lambda.

Bus stops, bus arrival, train service alerts and traffic incidents. Every call sends the AccountKey header
and is cached for about as long as LTA refreshes the data. Failures raise LtaError subclasses so callers can
degrade (plan without live data) rather than return a 500.
"""
import copy
import dataclasses
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator

import httpx

from .cache import JsonCache
from .config import Settings

log = logging.getLogger("journey.lta")

SST = timezone(timedelta(hours=8))          # Singapore has no daylight saving; LTA's timestamps are in this zone
PAGE_SIZE = 500                             # LTA returns at most 500 rows per call; page with $skip


class LtaError(Exception):
    pass


class LtaUnavailable(LtaError):
    """LTA is down, slow or rate-limiting us. Retrying later may work."""


class LtaAuthError(LtaError):
    """The AccountKey is missing or rejected. Retrying will not help."""


# One scale for "how busy": buses report Load (SEA seats / SDA standing / LSD limited standing), platforms report
# CrowdLevel (l / m / h). Anything else, including LTA's blank, is None (unknown).
BUS_LOAD = {"SEA": "low", "SDA": "moderate", "LSD": "high"}
PLATFORM_LEVEL = {"l": "low", "m": "moderate", "h": "high"}
# OneMap's route letter on a train leg -> DataMall's TrainLine parameter.
LINE_CODES = {"NS": "NSL", "EW": "EWL", "CG": "CGL", "NE": "NEL", "CC": "CCL", "CE": "CEL", "DT": "DTL", "TE": "TEL",
              "BP": "BPL", "SE": "SLRT", "SW": "SLRT", "PE": "PLRT", "PW": "PLRT"}
FORECAST_STEP = timedelta(minutes=30)


def _float(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f != 0.0 else None          # buses with no GPS fix report 0.0/0.0


def every(freq: str | None) -> list[int] | None:
    """LTA's frequency "08-12" (minutes between buses) -> [8, 12]; "-", "" or zeros (no service then) -> None."""
    try:
        low, high = (int(x) for x in (freq or "").split("-"))
    except ValueError:
        return None
    return [low, high] if 0 < low <= high else None


def eta_minutes(estimated_arrival: str, now: datetime) -> int | None:
    """Whole minutes until arrival, rounded down; None if unparseable. Negative -> 0."""
    try:
        arrival = datetime.fromisoformat(estimated_arrival)
    except ValueError:
        return None
    if arrival.tzinfo is None:
        arrival = arrival.replace(tzinfo=timezone.utc)
    return max(0, int((arrival - now).total_seconds() // 60))


def eta_display(minutes: int | None) -> str | None:
    """LTA guide section 2.1: round down to the minute; under a minute is 'Arr'."""
    if minutes is None:
        return None
    return "Arr" if minutes < 1 else f"{minutes} min"


class LtaClient:
    def __init__(
        self,
        settings: Settings,
        redis_client=None,                      # optional: used only as a cache
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        http: httpx.Client | None = None,       # given (tests): always used, the data mode is ignored
        mode=None,                              # data_mode.DataMode; None = LTA_MODE, fixed
    ):
        self._s = settings
        self._cache = JsonCache(redis_client)
        self._sleep = sleep
        self._clock = clock
        self._mode = mode
        self._fixed_live = http is not None
        self._http = http or httpx.Client(
            base_url=settings.lta_base_url.rstrip("/") + "/", timeout=settings.lta_timeout_s)
        self._recorded_http: httpx.Client | None = None          # built on first use in saved-data mode
        self._stops_index: dict[str, tuple[float, dict[str, dict]]] = {}    # cache prefix -> (expiry, {code: stop})
        self._bus_memo: tuple[float, tuple | None] = (0.0, None)             # (expiry, the bus lists), see _bus_lists
        self._bus_lock = threading.Lock()

    @property
    def _recorded(self) -> bool:
        if self._fixed_live:
            return False
        return (self._mode.get("lta") if self._mode else self._s.lta_mode) == "recorded"

    @property
    def _cache_prefix(self) -> str:
        """Saved data must never be served from a cache filled by live data, or the other way round."""
        return "recorded:" if self._recorded else ""

    @property
    def _headers(self) -> dict[str, str]:
        return {"AccountKey": self._s.lta_account_key, "accept": "application/json"}

    def _client(self) -> httpx.Client:
        if not self._recorded:
            return self._http
        if self._recorded_http is None:
            from .replay import RecordedLta          # only loaded in saved-data mode (see replay.py)
            self._recorded_http = httpx.Client(base_url=self._s.lta_base_url.rstrip("/") + "/",
                                               transport=RecordedLta(clock=self._clock))
        return self._recorded_http

    def without_retries(self) -> "LtaClient":
        """The same client (connection, cache, data mode) but giving up after one attempt: for lookups that are
        only nice to have, such as the live data added to a plan."""
        quick = copy.copy(self)
        quick._s = dataclasses.replace(self._s, lta_retries=0)
        return quick

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        recorded = self._recorded
        if not self._s.lta_account_key and not recorded:
            raise LtaAuthError("LTA_API_KEY is not set")
        headers = self._headers
        http = self._client()
        last: Exception | None = None
        for attempt in range(self._s.lta_retries + 1):
            if attempt:
                self._sleep(0.5 * 2 ** (attempt - 1))
            try:
                r = http.get(path, params=params, headers=headers)
            except httpx.HTTPError as e:                      # timeouts, connection errors
                last = e
                continue
            if r.status_code in (401, 403):
                raise LtaAuthError(f"LTA rejected the AccountKey ({r.status_code})")
            if r.status_code == 429 or r.status_code >= 500:
                last = LtaUnavailable(f"LTA returned {r.status_code}")
                continue
            if r.status_code >= 400:
                raise LtaError(f"LTA returned {r.status_code} for {path}")
            if not r.content:
                return {}                                     # BusArrival is empty outside service hours
            try:
                return r.json()
            except ValueError as e:
                last = e
        log.warning("LTA %s failed after %d attempts: %s", path, self._s.lta_retries + 1, type(last).__name__)
        raise LtaUnavailable(f"LTA {path} unavailable") from last

    def bus_stops(self) -> list[dict]:
        """Every bus stop: {code, roadName, description, lat, lng}. ~5k rows, so cached for a day."""
        return self._cache.cached(f"{self._cache_prefix}bus-stops", self._s.bus_stops_ttl_s, self._load_bus_stops)

    def _load_bus_stops(self) -> list[dict]:
        stops: list[dict] = []
        skip = 0
        while True:
            rows = self._get("BusStops", {"$skip": skip}).get("value", [])
            stops.extend(
                {"code": r["BusStopCode"], "roadName": r.get("RoadName"),
                 "description": r.get("Description"), "lat": r.get("Latitude"), "lng": r.get("Longitude")}
                for r in rows)
            if len(rows) < PAGE_SIZE:
                return stops
            skip += PAGE_SIZE

    def bus_stop(self, code: str) -> dict | None:
        """One stop by code, from an in-memory index of bus_stops() (rebuilt when it is older than the cache)."""
        prefix = self._cache_prefix
        expires, index = self._stops_index.get(prefix, (0.0, {}))
        if time.monotonic() >= expires:
            index = {s["code"]: s for s in self.bus_stops()}
            self._stops_index[prefix] = (time.monotonic() + self._s.bus_stops_ttl_s, index)
        return index.get(code)

    # Bus services: where a bus is going and how often it comes (for the easy-read wording)
    #
    # Static lists (BusServices ~800 rows, BusRoutes ~27k rows, about 9 s to page through), so cached for a week
    # and, like station exits, always fetched from live LTA, also with saved data. Best-effort: no key or a
    # failure means the wording just leaves these details out. One thread loads them at a time (the startup
    # prefetch, usually); a plan made meanwhile goes without rather than waiting, and a failure is remembered for
    # BUS_RETRY_S so a slow LTA doesn't hold up every bus step of every plan.

    BUS_SERVICES_TTL_S = 7 * 24 * 3600
    BUS_MEMO_S = 3600                       # how long the lists stay in memory before the (Redis) cache is read
    BUS_RETRY_S = 600                       # after a failure, how long until the lists are fetched again

    def bus_service(self, service_no: str, board_code: str, alight_code: str) -> dict | None:
        """For `service_no` riding from one stop to the other: {"towards": the stop the bus is going to (its front
        sign), None for a loop service; "every": [low, high] minutes between buses in the daytime, or None}. None
        when the lists aren't to hand or LTA has no direction of the service that stops at both, in that order.
        A loop service needs no direction: it passes each stop pole once a loop, always the same way, so at the
        right pole (the wording has the traveller check its number) every bus of that number is the right one.
        (LTA's LoopDesc isn't what its sign shows either: "PTB2 BASEMENT" for bus 24.)"""
        lists = self._bus_lists()
        if lists is None:
            return None
        routes, services = lists
        for direction, stops in (routes.get(service_no) or {}).items():
            if board_code in stops and alight_code in stops[stops.index(board_code) + 1:]:
                info = (services.get(service_no) or {}).get(direction) or {}
                dest, origin = info.get("dest"), info.get("origin")
                loop = not dest or dest == origin
                towards = None if loop else (self.bus_stop(dest) or {}).get("description")
                return {"towards": towards, "every": info.get("every")}
        return None

    def warm_bus_services(self) -> None:
        """Fetch the lists ahead of the first plan that needs them (call it in a background thread)."""
        self._bus_lists()

    def _bus_lists(self) -> tuple[dict, dict] | None:
        """({service: {direction: [stop codes in order]}}, {service: {direction: {origin, dest, every}}}), kept
        in memory after the first read from the cache; None while another thread is loading them, or for
        BUS_RETRY_S after loading failed."""
        expires, lists = self._bus_memo
        if time.monotonic() < expires:
            return lists
        if not self._bus_lock.acquire(blocking=False):
            return None                                     # being loaded: this plan goes without
        try:
            try:
                lists = (self._cache.cached("bus-routes", self.BUS_SERVICES_TTL_S, self._load_bus_routes),
                         self._cache.cached("bus-services", self.BUS_SERVICES_TTL_S, self._load_bus_services))
                self._bus_memo = (time.monotonic() + self.BUS_MEMO_S, lists)
            except (LtaError, httpx.HTTPError, ValueError, KeyError, TypeError) as e:
                log.warning("bus services unavailable (%s): trying again in %d s", type(e).__name__, self.BUS_RETRY_S)
                lists = None
                self._bus_memo = (time.monotonic() + self.BUS_RETRY_S, None)
            return lists
        finally:
            self._bus_lock.release()

    def _live_pages(self, path: str) -> Iterator[dict]:
        """Every row of a paged LTA list, from live LTA even with saved data and without _get's retries: the
        caller is best-effort, so one failure just leaves the details out."""
        if not self._s.lta_account_key:
            raise LtaAuthError("LTA_API_KEY is not set")
        skip = 0
        while True:
            r = self._http.get(path, params={"$skip": skip}, headers=self._headers)
            r.raise_for_status()
            rows = r.json().get("value", [])
            yield from rows
            if len(rows) < PAGE_SIZE:
                return
            skip += PAGE_SIZE

    def _load_bus_routes(self) -> dict:
        routes: dict[str, dict[str, list]] = {}
        for r in sorted(self._live_pages("BusRoutes"), key=lambda r: (r["ServiceNo"], r["Direction"], r["StopSequence"])):
            routes.setdefault(r["ServiceNo"], {}).setdefault(str(r["Direction"]), []).append(r["BusStopCode"])
        return routes

    def _load_bus_services(self) -> dict:
        services: dict[str, dict[str, dict]] = {}
        for r in self._live_pages("BusServices"):
            services.setdefault(r["ServiceNo"], {})[str(r["Direction"])] = {
                "origin": r.get("OriginCode"), "dest": r.get("DestinationCode"),
                "every": every(r.get("AM_Offpeak_Freq"))}            # daytime: 08:31-16:59
        return services

    def bus_arrival(self, bus_stop_code: str, service_no: str | None = None) -> dict:
        """Next buses at a stop. `services` is empty outside service hours (not an error)."""
        raw = self._cache.cached(
            f"{self._cache_prefix}bus-arrival:{bus_stop_code}:{service_no or '*'}", self._s.bus_arrival_ttl_s,
            lambda: self._get("v3/BusArrival", {
                "BusStopCode": bus_stop_code, **({"ServiceNo": service_no} if service_no else {})}))
        now = self._clock()
        services = []
        for svc in raw.get("Services", []):
            buses = []
            for tag in ("NextBus", "NextBus2", "NextBus3"):
                bus = svc.get(tag) or {}
                if not bus.get("EstimatedArrival"):
                    continue                                  # blank when fewer than 3 buses are running
                minutes = eta_minutes(bus["EstimatedArrival"], now)
                buses.append({
                    "estimatedArrival": bus["EstimatedArrival"],
                    "minutes": minutes,
                    "display": eta_display(minutes),
                    "load": bus.get("Load") or None,                  # SEA / SDA / LSD
                    "crowd": BUS_LOAD.get(bus.get("Load")),           # low / moderate / high / None
                    "wheelchairAccessible": bus.get("Feature") == "WAB",
                    "monitored": bus.get("Monitored") == 1,           # 0 = timetable, 1 = GPS
                    "type": bus.get("Type") or None,                  # SD / DD / BD
                    "lat": _float(bus.get("Latitude")),
                    "lng": _float(bus.get("Longitude")),
                })
            if buses:
                services.append({"serviceNo": svc.get("ServiceNo"), "operator": svc.get("Operator"), "buses": buses})
        return {"busStopCode": bus_stop_code, "services": services}

    def train_alerts(self) -> dict:
        """{"status": 1|2, "segments": [...], "messages": [...]}; status 2 = disrupted."""
        raw = self._cache.cached(f"{self._cache_prefix}train-alerts", self._s.train_alerts_ttl_s,
                                 lambda: self._get("TrainServiceAlerts"))
        value = raw.get("value") or {}
        return {
            "status": value.get("Status", 1),
            "segments": [
                {"line": seg.get("Line"), "direction": seg.get("Direction"),
                 "stations": [s for s in (seg.get("Stations") or "").split(",") if s],
                 "freePublicBus": seg.get("FreePublicBus") or None,
                 "freeMrtShuttle": seg.get("FreeMRTShuttle") or None}
                for seg in value.get("AffectedSegments", [])],
            "messages": [{"content": m.get("Content"), "createdDate": m.get("CreatedDate")}
                         for m in value.get("Message", [])],
        }

    def traffic_incidents(self) -> list[dict]:
        raw = self._cache.cached(f"{self._cache_prefix}traffic-incidents", self._s.traffic_incidents_ttl_s,
                                 lambda: self._get("TrafficIncidents"))
        return [{"type": r.get("Type"), "lat": r.get("Latitude"), "lng": r.get("Longitude"),
                 "message": r.get("Message")} for r in raw.get("value", [])]

    def platform_crowd(self, line: str) -> dict[str, str]:
        """Right now: {station code: "low"|"moderate"|"high"} for one line (DataMall TrainLine code, e.g. NEL).
        Stations LTA reports no level for are left out."""
        raw = self._cache.cached(f"{self._cache_prefix}platform-crowd:{line}", self._s.platform_crowd_ttl_s,
                                 lambda: self._get("PCDRealTime", {"TrainLine": line}))
        return {r["Station"]: PLATFORM_LEVEL[r["CrowdLevel"]] for r in raw.get("value", [])
                if r.get("CrowdLevel") in PLATFORM_LEVEL}

    def platform_crowd_forecast(self, line: str, when: datetime) -> dict[str, str]:
        """The forecast for `when`: {station code: level} from the 30-minute interval that contains it."""
        raw = self._cache.cached(f"{self._cache_prefix}platform-forecast:{line}", self._s.platform_forecast_ttl_s,
                                 lambda: self._get("PCDForecast", {"TrainLine": line}))
        levels: dict[str, str] = {}
        for day in raw.get("value", []):
            for station in day.get("Stations", []):
                for interval in station.get("Interval", []):
                    try:
                        start = datetime.fromisoformat(interval["Start"])
                    except (KeyError, ValueError):
                        continue
                    if start <= when < start + FORECAST_STEP and interval.get("CrowdLevel") in PLATFORM_LEVEL:
                        levels[station["Station"]] = PLATFORM_LEVEL[interval["CrowdLevel"]]
        return levels
