"""Saved-data ("recorded") mode: answers OneMap and LTA requests from files in app/saved_data/ instead of the internet.

The OneMap token expires every few days, so a demo can run on responses captured earlier (scripts/capture_data.py).
Each class is an httpx transport, so JourneyClient and LtaClient make the very same calls and parse the very same
bodies as in live mode; only the other end of the wire is a file. Standard library + httpx only, so the disruption
Lambda can use RecordedLta without importing any API code.

Times are moved to "now": LTA bus arrivals are timestamps from the capture time, and OneMap itineraries from the
day they were planned, so without that every bus would show "Arr" and every trip would start in the past.
"""
import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import httpx

from .geo import distance_m

DATA_DIR = Path(__file__).parent / "saved_data"      # gitignored: a fresh clone runs scripts/capture_data.py first
SST = timezone(timedelta(hours=8))
MATCH_RADIUS_M = 150            # an endpoint within this distance of a saved one counts as the same place
TIME_KEYS = ("startTime", "endTime", "arrival", "departure", "date")        # OneMap epoch-millisecond fields


class RecordedDataMissing(Exception):
    """The requested trip was never recorded. The message lists what was, so the page can offer it."""


MISSING = ("No saved data in app/saved_data. Run `python scripts/capture_data.py --target both` (needs the API "
           "keys), or switch to live data.")


@lru_cache(maxsize=1)
def load_manifest(directory: Path = DATA_DIR) -> dict:
    return _read(directory, "manifest.json")


def _read(directory: Path, name: str) -> Any:
    try:
        return json.loads((directory / name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not (directory / "manifest.json").exists():
            raise RecordedDataMissing(MISSING) from None
        raise


def _json(request: httpx.Request, body: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body, request=request)



def _point(text: str) -> tuple[float, float]:
    lat, lng = text.split(",")
    return float(lat), float(lng)


def _shift_times(node: Any, delta_ms: int) -> Any:
    if isinstance(node, dict):
        return {k: (v + delta_ms if k in TIME_KEYS and isinstance(v, int) and v > 10**11 else _shift_times(v, delta_ms))
                for k, v in node.items()}
    if isinstance(node, list):
        return [_shift_times(v, delta_ms) for v in node]
    return node


def describe_saved_trips(directory: Path = DATA_DIR) -> str:
    return "; ".join(t["name"] for t in load_manifest(directory)["trips"])


class RecordedOneMap(httpx.BaseTransport):
    """Place search and public-transport routes from the saved OneMap responses."""

    def __init__(self, directory: Path = DATA_DIR):
        self._dir = directory
        self._manifest = load_manifest(directory)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if request.url.path.endswith("/elastic/search"):
            return _json(request, self._search(params.get("searchVal", "")))
        if request.url.path.endswith("/routingsvc/route"):
            return _json(request, self._route(params))
        return _json(request, {"error": "not saved"}, 404)

    def _search(self, query: str) -> dict:
        words = query.lower().split()
        rows = [r for r in _read(self._dir, "places.json")
                if all(w in f"{r.get('SEARCHVAL', '')} {r.get('ADDRESS', '')}".lower() for w in words)]
        return {"found": len(rows), "totalNumPages": 1, "pageNum": 1, "results": rows}

    def _route(self, params: dict) -> dict:
        start, end = _point(params["start"]), _point(params["end"])
        wanted = params.get("mode", "TRANSIT")
        recordings = [r for r in self._manifest["routes"]
                      if distance_m(start, tuple(r["from"])) <= MATCH_RADIUS_M
                      and distance_m(end, tuple(r["to"])) <= MATCH_RADIUS_M]
        if not recordings:
            raise RecordedDataMissing(
                "That trip is not in the saved demo data. Saved trips: "
                f"{describe_saved_trips(self._dir)}. Pick one of those, or switch to live data (needs a valid OneMap login).")
        by_mode = {r["mode"]: r for r in recordings}
        if wanted in by_mode:
            body = _read(self._dir, by_mode[wanted]["file"])
        else:
            # No separate recording for this mode: filter the same trip's TRANSIT one. OneMap's RAIL means
            # "no bus", BUS means "no train", so keep the itineraries that satisfy that.
            base = by_mode.get("TRANSIT") or next(iter(by_mode.values()))
            body = _read(self._dir, base["file"])
            banned = {"RAIL": {"BUS"}, "BUS": {"SUBWAY", "RAIL", "TRAM"}}.get(wanted, set())
            plan = dict(body["plan"])
            plan["itineraries"] = [it for it in plan.get("itineraries") or []
                                   if not banned & {leg.get("mode") for leg in it.get("legs", [])}]
            body = {**body, "plan": plan}
        return self._at_requested_time(self._truncate(body, params), params)

    @staticmethod
    def _truncate(body: dict, params: dict) -> dict:
        try:
            count = int(params["numItineraries"])
        except (KeyError, ValueError):
            return body
        plan = dict(body["plan"])
        plan["itineraries"] = (plan.get("itineraries") or [])[:count]
        return {**body, "plan": plan}

    @staticmethod
    def _at_requested_time(body: dict, params: dict) -> dict:
        """Move every time so the trip is planned for the date and time that was asked for."""
        recorded = (body.get("plan") or {}).get("date")
        try:
            asked = datetime.strptime(f"{params['date']} {params['time']}", "%m-%d-%Y %H:%M:%S").replace(tzinfo=SST)
        except (KeyError, ValueError):
            return body
        if not isinstance(recorded, int):
            return body
        return _shift_times(body, int(asked.timestamp() * 1000) - recorded)


class RecordedLta(httpx.BaseTransport):
    """LTA DataMall responses from the saved files; bus arrival times are moved to 'now'."""

    def __init__(self, directory: Path = DATA_DIR, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._dir = directory
        self._clock = clock
        self._lta = load_manifest(directory)["lta"]
        self._recorded_at = datetime.fromisoformat(self._lta["recordedAt"])

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        if path.endswith("/v3/BusArrival"):
            return _json(request, self._bus_arrival(params.get("BusStopCode", ""), params.get("ServiceNo")))
        if path.endswith("/BusStops"):
            rows = _read(self._dir, "lta/bus_stops.json")
            skip = int(params.get("$skip", 0))
            return _json(request, {"value": rows[skip:skip + 500]})
        if path.endswith("/PCDRealTime"):
            return _json(request, self._platform_crowd(params.get("TrainLine", "")))
        if path.endswith("/PCDForecast"):
            return _json(request, self._platform_forecast(params.get("TrainLine", "")))
        if path.endswith("/TrainServiceAlerts"):
            return _json(request, _read(self._dir, "lta/train_alerts.json"))
        if path.endswith("/TrafficIncidents"):
            return _json(request, _read(self._dir, "lta/traffic_incidents.json"))
        return _json(request, {"error": "not saved"}, 404)

    def _platform_crowd(self, line: str) -> dict:
        """The saved levels, with their 10-minute window moved to the one containing 'now'."""
        if line not in self._lta.get("crowdLines", []):
            return {"value": []}                                   # a line nothing was saved for
        body = _read(self._dir, f"lta/platform_crowd_{line}.json")
        rows = body.get("value", [])
        if not rows:
            return body
        now = self._clock().astimezone(SST)
        window = now.replace(minute=now.minute // 10 * 10, second=0, microsecond=0)
        offset = window - datetime.fromisoformat(rows[0]["StartTime"])
        return {**body, "value": [{**r, "StartTime": (datetime.fromisoformat(r["StartTime"]) + offset).isoformat(),
                                   "EndTime": (datetime.fromisoformat(r["EndTime"]) + offset).isoformat()} for r in rows]}

    def _platform_forecast(self, line: str) -> dict:
        """The saved forecast, moved by whole days so it is for today (a recording is of one day's forecast)."""
        if line not in self._lta.get("crowdLines", []):
            return {"value": []}
        body = _read(self._dir, f"lta/platform_forecast_{line}.json")
        days = timedelta(days=(self._clock().astimezone(SST).date() - self._recorded_at.astimezone(SST).date()).days)
        shift = lambda text: (datetime.fromisoformat(text) + days).isoformat()
        return {**body, "value": [{**day, "Date": shift(day["Date"]), "Stations": [
            {**s, "Interval": [{**i, "Start": shift(i["Start"])} for i in s["Interval"]]} for s in day["Stations"]]}
            for day in body.get("value", [])]}

    def _bus_arrival(self, code: str, service_no: str | None) -> dict:
        if code not in self._lta["busArrivalStops"]:
            return {"BusStopCode": code, "Services": []}        # what LTA returns for a stop with nothing running
        body = _read(self._dir, f"lta/bus_arrival_{code}.json")
        offset = self._clock() - self._recorded_at
        services = []
        for svc in body.get("Services", []):
            if service_no and svc.get("ServiceNo") != service_no:
                continue
            svc = dict(svc)
            for tag in ("NextBus", "NextBus2", "NextBus3"):
                bus = dict(svc.get(tag) or {})
                if bus.get("EstimatedArrival"):
                    bus["EstimatedArrival"] = (datetime.fromisoformat(bus["EstimatedArrival"]) + offset).isoformat(
                        timespec="seconds")
                svc[tag] = bus
            services.append(svc)
        return {**body, "Services": services}
