# drive one scenario through the real services so the caregiver app shows it

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from tools.simulator.route import build_route
from tools.simulator.scenarios import generate, strip_labels

# fields the journey service sets itself
SERVICE_FIELDS = ("routeId", "version", "updatedAt")

# method url body to parsed json
Call = Callable[[str, str, Optional[dict]], object]


# scenario timestamp as a datetime
def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# an http error with the service answer
class ServiceError(Exception):
    def __init__(self, url: str, status: int, body: str):
        super().__init__(f"{url} answered {status}: {body[:200]}")
        self.status = status


# addresses of the three public services
@dataclass
class Endpoints:
    journey: str = "http://localhost:8006"
    checkin: str = "http://localhost:8001"
    alert: str = "http://localhost:8005"


# json request with the caregiver header the local services trust
def http_call(caregiver: str, timeout_s: float = 30) -> Call:
    def call(method: str, url: str, body: Optional[dict]) -> object:
        data = None if body is None else json.dumps(body).encode()
        headers = {"content-type": "application/json", "X-Dev-User": caregiver}
        req = urllib.request.Request(url, data, headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise ServiceError(url, exc.code, exc.read().decode(errors="replace")) from exc
        return json.loads(raw) if raw else None

    return call


# one simulated trip from route to last check-in
class LiveRun:
    def __init__(self, endpoints: Endpoints, caregiver: str, call: Call,
                 sleep: Callable[[float], None] = time.sleep, now: Callable[[], datetime] = None):
        self._urls = endpoints
        self._caregiver = caregiver
        self._call = call
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.traveller_id: Optional[str] = None
        self.trip_id: Optional[str] = None

    # given traveller or the caregiver first one or a new one
    def choose_traveller(self, traveller_id: Optional[str] = None, name: str = "Sim Alex") -> str:
        if traveller_id:
            self.traveller_id = traveller_id
            return traveller_id
        url = f"{self._urls.alert}/api/travellers"
        try:
            mine = self._call("GET", url, None)
            self.traveller_id = mine[0]["travellerId"] if mine else self._call("POST", url, {"name": name})["travellerId"]
        except ServiceError as exc:
            # login off as on eks so profiles need a fixed traveller
            if exc.status == 401:
                raise SystemExit("the Alert Service has login off, so pass --traveller U-...") from exc
            raise
        return self.traveller_id

    # route approved and trip started so the detectors have it in redis
    def start_trip(self) -> int:
        route = {k: v for k, v in build_route().items() if k not in SERVICE_FIELDS}
        route.update(travellerId=self.traveller_id, caregiverId=self._caregiver)
        saved = self._call("POST", f"{self._urls.journey}/api/routes", route)
        self._call("POST", f"{self._urls.journey}/api/routes/{saved['routeId']}/approve", {})
        date = self._now().date().isoformat()
        trip = self._call("POST", f"{self._urls.journey}/api/trips", {"routeId": saved["routeId"], "date": date})
        self.trip_id = trip["tripId"]
        self._call("POST", f"{self._urls.journey}/api/trips/{self.trip_id}/start", {})
        return saved["version"]

    # post every check-in and return the labelled ones sent
    def send(self, scenario: str, route_version: int, interval_s: float, seed: int, browser: bool,
             on_sent: Callable[[dict], None] = lambda c: None) -> List[dict]:
        checkins = generate(scenario, seed, browser=browser)
        offsets = [(_parse(c["timestamp"]) - _parse(checkins[0]["timestamp"])).total_seconds() for c in checkins]
        # keep the scenario spacing however fast they are sent, so the last is stamped as it goes out
        first = self._now() - timedelta(seconds=offsets[-1] - interval_s * (len(checkins) - 1))
        for i, labelled in enumerate(checkins):
            if i:
                self._sleep(interval_s)
            at = first + timedelta(seconds=offsets[i])
            body = strip_labels(labelled)
            body.update(tripId=self.trip_id, travellerId=self.traveller_id, routeVersion=route_version,
                        timestamp=at.strftime("%Y-%m-%dT%H:%M:%SZ"))
            self._call("POST", f"{self._urls.checkin}/api/checkins", body)
            on_sent(labelled)
        return checkins

    # incidents raised for this trip
    def incidents(self) -> List[Dict]:
        found = self._call("GET", f"{self._urls.alert}/api/incidents?travellerId={self.traveller_id}", None)
        return [i for i in found if f"#{self.trip_id}#" in i["incidentId"]]

    def end_trip(self) -> None:
        self._call("POST", f"{self._urls.journey}/api/trips/{self.trip_id}/end", {})
