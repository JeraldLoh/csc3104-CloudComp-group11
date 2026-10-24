"""LTA DataMall client (API User Guide v6.10), shared by the API container and the disruption Lambda.

Bus stops, bus arrival, train service alerts and traffic incidents. Every call sends the AccountKey header
and is cached for about as long as LTA refreshes the data. Failures raise LtaError subclasses so callers can
degrade (plan without live data) rather than return a 500.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

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
        http: httpx.Client | None = None,
    ):
        self._s = settings
        self._cache = JsonCache(redis_client)
        self._sleep = sleep
        self._clock = clock
        self._recorded = http is None and settings.lta_mode == "recorded"
        if self._recorded:
            from .replay import RecordedLta          # only loaded in saved-data mode (see replay.py)
            http = httpx.Client(base_url=settings.lta_base_url.rstrip("/") + "/", transport=RecordedLta(clock=clock))
        self._http = http or httpx.Client(
            base_url=settings.lta_base_url.rstrip("/") + "/", timeout=settings.lta_timeout_s)
        # Saved data must never be served from a cache filled by live data, or the other way round.
        self._cache_prefix = "recorded:" if self._recorded else ""

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        if not self._s.lta_account_key and not self._recorded:
            raise LtaAuthError("LTA_API_KEY is not set")
        headers = {"AccountKey": self._s.lta_account_key, "accept": "application/json"}
        last: Exception | None = None
        for attempt in range(self._s.lta_retries + 1):
            if attempt:
                self._sleep(0.5 * 2 ** (attempt - 1))
            try:
                r = self._http.get(path, params=params, headers=headers)
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
        return next((s for s in self.bus_stops() if s["code"] == code), None)

    def bus_arrival(self, bus_stop_code: str, service_no: str | None = None) -> dict:
        """Next buses at a stop. `services` is empty outside service hours (not an error)."""
        raw = self._cache.cached(
            f"bus-arrival:{bus_stop_code}:{service_no or '*'}", self._s.bus_arrival_ttl_s,
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
        raw = self._cache.cached("train-alerts", self._s.train_alerts_ttl_s, lambda: self._get("TrainServiceAlerts"))
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
        raw = self._cache.cached("traffic-incidents", self._s.traffic_incidents_ttl_s,
                                 lambda: self._get("TrafficIncidents"))
        return [{"type": r.get("Type"), "lat": r.get("Latitude"), "lng": r.get("Longitude"),
                 "message": r.get("Message")} for r in raw.get("value", [])]

    def platform_crowd(self, line: str) -> dict[str, str]:
        """Right now: {station code: "low"|"moderate"|"high"} for one line (DataMall TrainLine code, e.g. NEL).
        Stations LTA reports no level for are left out."""
        raw = self._cache.cached(f"platform-crowd:{line}", self._s.platform_crowd_ttl_s,
                                 lambda: self._get("PCDRealTime", {"TrainLine": line}))
        return {r["Station"]: PLATFORM_LEVEL[r["CrowdLevel"]] for r in raw.get("value", [])
                if r.get("CrowdLevel") in PLATFORM_LEVEL}

    def platform_crowd_forecast(self, line: str, when: datetime) -> dict[str, str]:
        """The forecast for `when`: {station code: level} from the 30-minute interval that contains it."""
        raw = self._cache.cached(f"platform-forecast:{line}", self._s.platform_forecast_ttl_s,
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
