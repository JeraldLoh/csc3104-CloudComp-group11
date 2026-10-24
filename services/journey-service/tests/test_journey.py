"""Tests for JourneyClient and the HTTP API on top of it.

OneMap/LTA responses come from tests/fixtures: real captures made with scripts/capture_data.py
(lta_*.json, onemap_*.json) plus a few hand-built responses for cases that can't be triggered live
(an MRT outage, no buses at night, upstream errors).
"""
import base64
import hmac
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fakeredis
import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.auth import Authenticator, Caller, Unauthorized
from app.config import Settings
from app.disruption import DisruptionChecker
from app.journey_client import JourneyClient, decode_polyline, distance_to_path_m
from app.lta import SST, LtaClient
from app.main import create_app
from app.models import RouteIn
from app.replay import MATCH_RADIUS_M, distance_m
from app.route_store import RouteConflict, RouteNotFound
from app.trip_store import TripConflict

LTA = "https://datamall2.mytransport.sg/ltaodataservice"
ONEMAP = "https://www.onemap.gov.sg"
ROUTE = f"{ONEMAP}/api/public/routingsvc/route"
SEARCH = f"{ONEMAP}/api/common/elastic/search"
FIXTURES = Path(__file__).parent / "fixtures"

SETTINGS = Settings(lta_account_key="lta", onemap_api_key="om", onemap_email="", onemap_password="",
                    lta_retries=0, onemap_retries=0)
NOW = datetime(2026, 10, 22, 8, 0, tzinfo=SST)                  # planning "now": 8:00 AM tomorrow
LTA_NOW = datetime(2024, 8, 14, 8, 40, tzinfo=timezone.utc)     # 16:40 SST, matches the guide's samples


# --- helpers -----------------------------------------------------------------------


def onemap_fixture(name: str) -> dict:
    """The response body saved by capture_data.py (it also stores the HTTP status)."""
    return json.loads((FIXTURES / f"onemap_{name}").read_text(encoding="utf-8"))["body"]


def make(clock=NOW, settings=SETTINGS, redis=None) -> JourneyClient:
    return JourneyClient(settings, redis_client=redis, sleep=lambda _: None,
                         clock=lambda: clock.astimezone(timezone.utc))


def mock_onemap(name: str):
    return respx.get(ROUTE).mock(return_value=httpx.Response(200, json=onemap_fixture(name)))


def mock_lta(arrival=None, alerts=None, incidents=None, crowd=None, forecast=None):
    respx.get(f"{LTA}/PCDRealTime").mock(return_value=httpx.Response(200, json=crowd or {"value": []}))
    respx.get(f"{LTA}/PCDForecast").mock(return_value=httpx.Response(200, json=forecast or {"value": []}))
    respx.get(f"{LTA}/v3/BusArrival").mock(return_value=httpx.Response(200, json=arrival or {"Services": []}))
    respx.get(f"{LTA}/TrainServiceAlerts").mock(return_value=httpx.Response(200, json=alerts or {
        "value": {"Status": 1, "AffectedSegments": [], "Message": []}}))
    respx.get(f"{LTA}/TrafficIncidents").mock(return_value=httpx.Response(200, json=incidents or {"value": []}))


def plan(**kw):
    return make().plan((1.37, 103.8486), (1.3326, 103.8474), NOW, **kw)["options"]


def first_leg(name: str, mode: str) -> dict:
    return next(l for l in onemap_fixture(name)["plan"]["itineraries"][0]["legs"] if l["mode"] == mode)


# =====================================================================================
# Helpers: ETAs, polylines, distances, names
# =====================================================================================


def test_decode_polyline_known_example():
    # The worked example from Google's polyline algorithm documentation.
    assert decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@") == [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]


def test_distance_to_path():
    path = [(1.3000, 103.8000), (1.3000, 103.8100)]          # ~1.1 km east-west line
    assert distance_to_path_m(1.3000, 103.8050, path) < 1
    assert 105 < distance_to_path_m(1.3010, 103.8050, path) < 115   # ~111 m north of it
    assert distance_to_path_m(1.3, 103.8, []) == float("inf")


# =====================================================================================
# LTA DataMall: bus stops, bus arrival, train alerts, traffic incidents
# =====================================================================================

def bus(eta, monitored=1, lat="1.3154918333333334", lng="103.9059125", load="SEA"):
    return {"OriginCode": "77009", "DestinationCode": "77009", "EstimatedArrival": eta,
            "Monitored": monitored, "Latitude": lat, "Longitude": lng, "VisitNumber": "1",
            "Load": load, "Feature": "WAB", "Type": "SD"}


EMPTY_BUS = {k: "" for k in ("OriginCode", "DestinationCode", "EstimatedArrival", "Latitude",
                             "Longitude", "VisitNumber", "Load", "Feature", "Type")} | {"Monitored": 0}


@respx.mock
def test_bus_arrival_normalises_and_skips_blank_buses():
    route = respx.get(f"{LTA}/v3/BusArrival").mock(return_value=httpx.Response(200, json={
        "BusStopCode": "83139",
        "Services": [
            {"ServiceNo": "15", "Operator": "GAS", "NextBus": bus("2024-08-14T16:41:48+08:00"),
             "NextBus2": bus("2024-08-14T16:49:22+08:00"), "NextBus3": EMPTY_BUS},
            {"ServiceNo": "150", "Operator": "SBST",
             "NextBus": bus("2024-08-14T16:55:22+08:00", monitored=0, lat="0.0", lng="0.0"),
             "NextBus2": EMPTY_BUS, "NextBus3": EMPTY_BUS},
        ],
    }))
    out = make(clock=LTA_NOW).bus_arrival("83139")

    assert route.calls.last.request.headers["AccountKey"] == "lta"
    assert route.calls.last.request.url.params["BusStopCode"] == "83139"
    s15, s150 = out["services"]
    assert [b["minutes"] for b in s15["buses"]] == [1, 9]
    assert s15["buses"][0]["display"] == "1 min"
    assert s15["buses"][0]["wheelchairAccessible"] is True
    assert s15["buses"][0]["monitored"] is True
    assert s150["buses"][0]["monitored"] is False
    assert s150["buses"][0]["lat"] is None          # 0.0/0.0 means no GPS fix
    assert len(s150["buses"]) == 1


@respx.mock
def test_train_alerts_disruption():
    respx.get(f"{LTA}/TrainServiceAlerts").mock(return_value=httpx.Response(200, json={"value": {
        "Status": 2,
        "AffectedSegments": [{"Line": "NEL", "Direction": "HarbourFront", "Stations": "NE9,NE8,NE7,NE6",
                              "FreePublicBus": "", "FreeMRTShuttle": "", "MRTShuttleDirection": ""}],
        "Message": [{"Content": "1710hrs: NEL - No train service ...", "CreatedDate": "2017-12-01 17:54:21"}],
    }}))
    out = make().train_alerts()
    assert out["status"] == 2
    assert out["segments"][0] == {"line": "NEL", "direction": "HarbourFront",
                                  "stations": ["NE9", "NE8", "NE7", "NE6"],
                                  "freePublicBus": None, "freeMrtShuttle": None}
    assert out["messages"][0]["createdDate"] == "2017-12-01 17:54:21"


# --- real captures from the live API (scripts/capture_data.py, 2026-10-21) ---


# --- caching, retries, failures ---


@respx.mock
def test_redis_failure_is_a_cache_miss_not_an_error():
    class BrokenRedis:
        def get(self, *a, **k):
            raise ConnectionError("down")

        def set(self, *a, **k):
            raise ConnectionError("down")

    route = respx.get(f"{LTA}/TrainServiceAlerts").mock(return_value=httpx.Response(200, json={
        "value": {"Status": 1, "AffectedSegments": [], "Message": []}}))
    client = make(redis=BrokenRedis())
    assert client.train_alerts()["status"] == 1
    client.train_alerts()
    assert route.call_count == 2            # nothing cached, but nothing failed either


# =====================================================================================
# OneMap: place search
# =====================================================================================

def mock_search():
    return respx.get(SEARCH).mock(return_value=httpx.Response(200, json=onemap_fixture("search.json")))


@respx.mock
def test_search_returns_clean_places():
    mock_search()
    places = make().search_places("Tampines Mall")
    assert len(places) == 3
    assert places[0] == {
        "name": "Tampines Mall",
        "address": "4 Tampines Central 5 Tampines Mall Singapore 529510",
        "postalCode": "529510",
        "lat": 1.352527372785196,
        "lng": 103.9446987510717,
    }
    assert places[1]["postalCode"] is None          # OneMap's "NIL" becomes null


# =====================================================================================
# Planning: OneMap itineraries -> contract steps
# =====================================================================================

@respx.mock
def test_bus_itinerary_maps_to_contract_steps():
    mock_onemap("route_pt_bus_only_short.json")
    mock_lta()
    options = plan()
    raw = onemap_fixture("route_pt_bus_only_short.json")["plan"]["itineraries"]

    assert len(options) == len(raw)
    opt = options[0]
    assert opt["summary"]["durationS"] == raw[0]["duration"]
    assert opt["summary"]["startTime"].endswith("Z")
    assert len(opt["steps"]) == len(raw[0]["legs"])
    bus_steps = [s for s in opt["steps"] if s["mode"] == "bus"]
    assert bus_steps
    s = bus_steps[0]
    expected = first_leg("route_pt_bus_only_short.json", "BUS")
    assert s["line"] == expected["route"]
    assert s["from"]["code"] == expected["from"]["stopCode"]
    assert s["instruction"].startswith(f"Take bus {s['line']} from")
    assert set(s) == {"stepIndex", "mode", "instruction", "easyRead", "from", "to", "line", "stopCount",
                      "expectedDurationS", "underground", "photoKey", "audioKey", "backup"}
    assert [x["stepIndex"] for x in opt["steps"]] == list(range(len(opt["steps"])))
    assert all(1.1 < lat < 1.5 and 103.5 < lng < 104.1 for lat, lng in opt["path"])
    assert len(opt["path"]) > 5


@respx.mock
def test_mrt_itinerary_maps_to_train_step():
    mock_onemap("route_pt_bus_mrt.json")
    mock_lta()
    opt = plan()[0]
    train = next(s for s in opt["steps"] if s["mode"] == "train")
    assert train["line"] == "NE"
    assert train["from"]["code"] == "NE17" and train["to"]["code"] == "NE6"
    assert train["from"]["name"] == "Punggol MRT Station"
    assert "North East Line" in train["instruction"]
    assert train["underground"] is None
    walk = opt["steps"][0]
    assert walk["mode"] == "walk" and walk["from"]["name"] is None     # no invented place names


@respx.mock
def test_request_to_onemap_uses_onemap_formats():
    route = mock_onemap("route_pt_bus_mrt.json")
    mock_lta()
    plan()
    p = route.calls.last.request.url.params
    assert p["routeType"] == "pt" and p["date"] == "10-22-2026" and p["time"] == "08:00:00"
    assert p["start"] == "1.37,103.8486"
    assert route.calls.last.request.headers["Authorization"] == "Bearer om"


# --- planner controls: mode, option count, walking limit ---


# =====================================================================================
# Planning: cross-check against LTA live data
# =====================================================================================

@respx.mock
def test_bus_leg_gets_next_buses():
    leg = first_leg("route_pt_bus_only_short.json", "BUS")
    mock_onemap("route_pt_bus_only_short.json")
    mock_lta(arrival={"Services": [{"ServiceNo": leg["route"], "Operator": "SBST", "NextBus": {
        "EstimatedArrival": "2026-10-22T08:04:30+08:00", "Monitored": 1, "Latitude": "1.3",
        "Longitude": "103.8", "Load": "SDA", "Feature": "WAB", "Type": "SD"}}]})
    entry = plan()[0]["live"][0]
    assert entry["serviceNo"] == leg["route"] and entry["busStopCode"] == leg["from"]["stopCode"]
    assert entry["nextBuses"] == [{"minutes": 4, "display": "4 min", "load": "SDA", "crowd": "moderate", "wheelchairAccessible": True}]
    assert entry["crowd"] == "moderate"


@respx.mock
def test_train_disruption_on_the_leg_is_flagged():
    mock_onemap("route_pt_bus_mrt.json")
    mock_lta(alerts={"value": {
        "Status": 2,
        "AffectedSegments": [{"Line": "NEL", "Direction": "HarbourFront", "Stations": "NE12,NE11,NE10",
                              "FreePublicBus": "", "FreeMRTShuttle": "", "MRTShuttleDirection": ""}],
        "Message": [{"Content": "NEL: no train service between Serangoon and Potong Pasir",
                     "CreatedDate": "2026-10-22 07:50:00"}]}})
    opt = plan()[0]
    w = next(w for w in opt["warnings"] if w["type"] == "train_disruption")
    train_index = next(s["stepIndex"] for s in opt["steps"] if s["mode"] == "train")
    assert w["stepIndex"] == train_index and w["severity"] == "high"
    assert w["affectedStations"] == ["NE10", "NE11", "NE12"]       # these are intermediate stops of the leg
    assert "Serangoon" in w["message"]


@respx.mock
def test_nearby_incident_flagged_and_far_incident_ignored():
    leg = first_leg("route_pt_bus_only_short.json", "BUS")
    path = decode_polyline(leg["legGeometry"]["points"])
    mid = path[len(path) // 2]
    mock_onemap("route_pt_bus_only_short.json")
    mock_lta(incidents={"value": [
        {"Type": "Accident", "Latitude": mid[0] + 0.0003, "Longitude": mid[1], "Message": "Accident near the route"},
        {"Type": "Roadwork", "Latitude": 1.45, "Longitude": 103.65, "Message": "Roadwork far away"},
    ]})
    inc = [w for w in plan()[0]["warnings"] if w["type"] == "traffic_incident"]
    assert inc and inc[0]["message"] == "Accident near the route"
    assert inc[0]["severity"] == "warning" and inc[0]["distanceM"] <= 200
    assert not [w for w in inc if "far away" in w["message"]]


@respx.mock
def test_lta_down_still_returns_options_with_warning():
    mock_onemap("route_pt_bus_only_short.json")
    for path in ("v3/BusArrival", "TrainServiceAlerts", "TrafficIncidents"):
        respx.get(f"{LTA}/{path}").mock(return_value=httpx.Response(503))
    options = plan()
    assert options
    assert all(any(w["type"] == "live_data_unavailable" for w in o["warnings"]) for o in options)
    assert all(o["live"] == [] for o in options)     # no arrivals known, so none are claimed


# --- OneMap failures ---


# =====================================================================================
# Disruption checker (what the Lambda runs)
# =====================================================================================

DISRUPTION_NOW = datetime(2026, 10, 21, 2, 5, 12, tzinfo=timezone.utc)

NEL_ALERT = {"value": {
    "Status": 2,
    "AffectedSegments": [{"Line": "NEL", "Direction": "HarbourFront", "Stations": "NE10,NE11,NE12",
                          "FreePublicBus": "", "FreeMRTShuttle": "", "MRTShuttleDirection": ""}],
    "Message": [{"Content": "NEL: no train service between Serangoon and Potong Pasir",
                 "CreatedDate": "2026-10-21 09:33:49"}],     # Singapore time = 01:33:49 UTC
}}


def stop(code, name):
    return {"code": code, "name": name}


def trip_route(traveller_id="U-042"):
    return {
        "routeId": "R-1", "travellerId": traveller_id,
        "steps": [
            {"stepIndex": 0, "mode": "walk", "from": stop(None, None), "to": stop("NE17", "Punggol MRT Station")},
            {"stepIndex": 1, "mode": "train", "line": "NE", "from": stop("NE17", "Punggol MRT Station"),
             "to": stop("NE6", "Dhoby Ghaut MRT Station"),
             "stops": [stop("NE17", "Punggol MRT Station"), stop("NE12", "Serangoon MRT Station"),
                       stop("NE11", "Woodleigh MRT Station"), stop("NE10", "Potong Pasir MRT Station"),
                       stop("NE6", "Dhoby Ghaut MRT Station")]},
            {"stepIndex": 2, "mode": "walk", "from": stop("NE6", "Dhoby Ghaut MRT Station"), "to": stop(None, None)},
        ],
    }


def checker_over(redis, settings=SETTINGS) -> DisruptionChecker:
    """The disruption checker the Lambda runs: just Redis, the LTA client and the settings (no API code)."""
    lta = LtaClient(settings, redis_client=redis, sleep=lambda _: None, clock=lambda: DISRUPTION_NOW)
    return DisruptionChecker(redis, lta, settings, clock=lambda: DISRUPTION_NOW)


class Harness:
    """A disruption checker over fake Redis, collecting the messages it would put on the queue."""

    def __init__(self, alert=NEL_ALERT, settings=SETTINGS):
        self.redis = fakeredis.FakeRedis(decode_responses=True)
        self.checker = checker_over(self.redis, settings)
        self.sent: list[dict] = []
        self.fail_next = False
        self.alert = alert

    def send(self, event):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("SQS down")
        self.sent.append(event)

    def add_trip(self, trip_id="T-001", step=0, route_json=None, traveller="U-042"):
        self.redis.sadd("active-trips", trip_id)
        if route_json is not False:
            self.redis.set(f"trip:{trip_id}:route", json.dumps(route_json or trip_route()))
        ctx = {"stepIndex": step}
        if traveller:
            ctx["travellerId"] = traveller
        self.redis.hset(f"trip:{trip_id}:context", mapping=ctx)

    def run(self, send=None):
        with respx.mock:
            respx.get(f"{LTA}/TrainServiceAlerts").mock(return_value=httpx.Response(200, json=self.alert))
            return self.checker.check(send or self.send)


def test_disruption_on_upcoming_train_step_is_sent_in_contract_format():
    h = Harness()
    h.add_trip(step=0)
    summary = h.run()
    assert summary["sent"] == 1 and summary["activeTrips"] == 1
    assert h.sent == [{
        "disruptionId": "D-NEL-20261021-0133",
        "source": "lta-datamall",
        "tripId": "T-001",
        "travellerId": "U-042",
        "affectedStepIndex": 1,
        "mode": "train",
        "line": "NEL",
        "affectedStations": ["Serangoon MRT Station", "Woodleigh MRT Station", "Potong Pasir MRT Station"],
        "message": "NEL: no train service between Serangoon and Potong Pasir",
        "detectedAt": "2026-10-21T02:05:12Z",
    }]


def test_same_disruption_is_not_sent_twice():
    h = Harness()
    h.add_trip()
    assert h.run()["sent"] == 1
    second = h.run()
    assert second["sent"] == 0 and second["duplicates"] == 1
    assert len(h.sent) == 1


def test_minor_delays_do_not_alert():
    h = Harness(alert={"value": {**NEL_ALERT["value"], "Status": 1}})
    h.add_trip()
    assert h.run()["sent"] == 0 and h.sent == []


def test_trip_on_a_different_line_is_not_alerted():
    r = trip_route()
    r["steps"][1]["stops"] = [stop("EW1", "Pasir Ris"), stop("EW2", "Tampines")]
    r["steps"][1]["from"], r["steps"][1]["to"] = stop("EW1", "Pasir Ris"), stop("EW2", "Tampines")
    h = Harness()
    h.add_trip(route_json=r)
    assert h.run()["sent"] == 0


def test_the_lambda_does_not_import_the_api_code():
    """The Lambda package must not need FastAPI, the login code (PyJWT), route stores or OneMap.
    (`cryptography` isn't checked: the `redis` package itself probes for it if it happens to be installed.)"""
    import subprocess
    import sys
    probe = ("import sys; import app.lambda_handler; "
             "print(','.join(sorted(m.split('.')[1] for m in sys.modules if m.startswith('app.') and m.count('.') == 1)));"
             "print(','.join(p for p in ('fastapi', 'starlette', 'pydantic', 'jwt') if p in sys.modules))")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True,
                         cwd=Path(__file__).resolve().parents[1]).stdout.splitlines()
    assert set(out[0].split(",")) == {"lambda_handler", "aws_resources", "config", "cache", "lta", "disruption"}
    assert out[1] == ""                                   # none of the API's third-party packages were loaded


# =====================================================================================
# Saved routes
# =====================================================================================

ROUTE_ID_FORMAT = re.compile(r"^R-[0-9A-F]{6}$")


def route_data(**over) -> dict:
    """A valid route as the server stores it (what RouteIn produces), modelled on contracts/examples/route.json."""
    data = {
        "travellerId": "U-042", "caregiverId": "G-003", "name": "Home to Clinic",
        "origin": {"name": "Home", "safeLocationId": "L-001", "lat": 1.404, "lng": 103.901},
        "destination": {"name": "Clinic", "safeLocationId": "L-004", "lat": 1.392, "lng": 103.895},
        "steps": [
            {"stepIndex": 0, "mode": "walk", "instruction": "Walk 200 m to Punggol Int", "easyRead": None,
             "from": {"name": "Home", "code": None, "lat": 1.404, "lng": 103.901},
             "to": {"name": "Punggol Int", "code": "65009", "lat": 1.4052, "lng": 103.9024},
             "line": None, "expectedDurationS": 240, "underground": False, "photoKey": None, "audioKey": None,
             "backup": None},
            {"stepIndex": 1, "mode": "bus", "instruction": "Take bus 190 to Opp Block 221", "easyRead": None,
             "from": {"name": "Punggol Int", "code": "65009", "lat": 1.4052, "lng": 103.9024},
             "to": {"name": "Opp Block 221", "code": "65221", "lat": 1.3935, "lng": 103.8962},
             "line": "190", "expectedDurationS": 900, "underground": False, "photoKey": None, "audioKey": None,
             "backup": {"instruction": "If bus 190 does not come in 20 minutes, take bus 83.", "line": "83"}},
        ],
        "path": [[1.404, 103.901], [1.4052, 103.9024], [1.3935, 103.8962], [1.392, 103.895]],
    }
    return {**data, **over}


def routes_client(clock=NOW, redis=None) -> JourneyClient:
    return make(clock=clock, redis=redis or fakeredis.FakeRedis())


def test_create_route_assigns_id_version_and_unapproved():
    route = routes_client().create_route(route_data())
    assert ROUTE_ID_FORMAT.match(route["routeId"])
    assert (route["version"], route["approved"]) == (1, False)
    assert route["updatedAt"] == "2026-10-22T00:00:00Z"            # NOW is 08:00 SST
    assert route["name"] == "Home to Clinic" and len(route["steps"]) == 2


def test_update_bumps_version_clears_approval_and_persists():
    client = routes_client()
    saved = client.create_route(route_data())
    client.approve_route(saved["routeId"])
    updated = client.update_route(saved["routeId"], route_data(name="Home to GP"), expected_version=1)
    assert (updated["version"], updated["approved"], updated["name"]) == (2, False, "Home to GP")
    assert client.get_route(saved["routeId"]) == updated
    assert updated["routeId"] == saved["routeId"]


def test_stale_update_is_rejected_and_changes_nothing():
    client = routes_client()
    saved = client.create_route(route_data())
    client.update_route(saved["routeId"], route_data(name="First edit"), expected_version=1)
    with pytest.raises(RouteConflict) as e:
        client.update_route(saved["routeId"], route_data(name="Second edit, from a stale copy"), expected_version=1)
    assert e.value.current_version == 2
    assert client.get_route(saved["routeId"])["name"] == "First edit"


def test_approve_keeps_version_and_is_idempotent():
    client = routes_client()
    saved = client.create_route(route_data())
    approved = client.approve_route(saved["routeId"])
    assert approved["approved"] is True and approved["version"] == 1
    assert client.approve_route(saved["routeId"]) == approved          # second approve changes nothing
    assert client.get_route(saved["routeId"])["approved"] is True
    with pytest.raises(RouteNotFound):
        client.approve_route("R-000000")


@respx.mock
def test_a_real_plan_option_can_be_saved_with_its_train_stations():
    mock_onemap("route_pt_bus_mrt.json")
    mock_lta()
    option = plan()[0]
    body = {"travellerId": "U-042", "caregiverId": "G-003", "name": "Punggol to Orchard",
            "origin": {"name": "Home", "lat": 1.4043, "lng": 103.9021},
            "destination": {"name": "Orchard", "lat": 1.3040, "lng": 103.8318},
            "steps": option["steps"], "path": option["path"]}
    parsed = RouteIn.model_validate(body).model_dump(by_alias=True)
    saved = routes_client().create_route(parsed)
    train = next(s for s in saved["steps"] if s["mode"] == "train")
    assert [s["code"] for s in train["stops"]][0] == "NE17" and len(train["stops"]) == 12   # `stops` survives saving
    assert "from" in saved["steps"][0] and "from_" not in saved["steps"][0]


# --- the two route stores behave the same (Redis stand-in vs the real DynamoDB one) ---


# =====================================================================================
# Trips
# =====================================================================================

def approved_route(client, **over) -> dict:
    route = client.create_route(route_data(**over))
    return client.approve_route(route["routeId"])


def trip_client(clock=NOW):
    r = fakeredis.FakeRedis()
    return make(clock=clock, redis=r), r


def test_trip_needs_an_approved_route():
    client, _ = trip_client()
    draft = client.create_route(route_data())
    with pytest.raises(TripConflict, match="approved"):
        client.create_trip(draft["routeId"], "2026-10-22")
    with pytest.raises(RouteNotFound):
        client.create_trip("R-000000", "2026-10-22")


def test_created_trip_pins_the_route_version_and_matches_the_contract_example():
    client, _ = trip_client()
    route = approved_route(client)
    trip = client.create_trip(route["routeId"], "2026-10-22")
    assert re.match(r"^T-[0-9A-F]{6}$", trip["tripId"])
    assert (trip["routeId"], trip["routeVersion"], trip["travellerId"]) == (route["routeId"], 1, "U-042")
    assert (trip["status"], trip["startedAt"], trip["endedAt"], trip["currentStepIndex"]) == ("planned", None, None, 0)
    example_path = Path(__file__).resolve().parents[3] / "contracts" / "examples" / "trip.json"
    if example_path.exists():
        assert list(trip) == list(json.loads(example_path.read_text(encoding="utf-8")))    # no expiresAt leaks out
    assert client.get_trip(trip["tripId"]) == trip


def test_cached_route_leaves_out_anything_personal():
    client, r = trip_client()
    body = route_data(name="Gran's house")
    body["steps"][0]["instruction"] = "Walk to Gran's house"
    body["steps"][0]["to"]["name"] = "Gran's house"
    body["steps"][0]["to"]["code"] = None                       # a caregiver's label, not a public stop
    route = client.approve_route(client.create_route(body)["routeId"])
    trip = client.start_trip(client.create_trip(route["routeId"], "2026-10-22")["tripId"])
    raw = r.get(f"trip:{trip['tripId']}:route").decode()
    for private in ("Gran", "Home", "Clinic", "Walk to", "easyRead", "instruction"):
        assert private not in raw
    cached = json.loads(raw)
    assert cached["steps"][0]["to"] == {"name": None, "code": None, "lat": 1.4052, "lng": 103.9024}
    assert cached["steps"][1]["to"]["name"] == "Opp Block 221"            # a public bus stop keeps its name


def test_end_trip_removes_the_redis_keys_and_is_idempotent():
    client, r = trip_client()
    trip = client.create_trip(approved_route(client)["routeId"], "2026-10-22")
    with pytest.raises(TripConflict, match="started"):
        client.end_trip(trip["tripId"])                         # can't end what hasn't started
    client.start_trip(trip["tripId"])
    ended = client.end_trip(trip["tripId"])
    assert ended["status"] == "completed" and ended["endedAt"] == "2026-10-22T00:00:00Z"
    assert not r.sismember("active-trips", trip["tripId"]) and not r.exists(f"trip:{trip['tripId']}:route")
    assert client.end_trip(trip["tripId"]) == ended
    with pytest.raises(TripConflict, match="finished"):
        client.start_trip(trip["tripId"])


@respx.mock
def test_a_started_trip_is_watched_by_the_disruption_checker_end_to_end():
    """Plan a real MRT route, save, approve, start the trip, then an MRT outage hits its stations."""
    mock_onemap("route_pt_bus_mrt.json")
    mock_lta(alerts=NEL_ALERT)
    option = plan()[0]
    client, r = trip_client(clock=DISRUPTION_NOW)
    body = {"travellerId": "U-042", "caregiverId": "G-003", "name": "Punggol to Orchard",
            "origin": {"name": "Home", "lat": 1.4043, "lng": 103.9021},
            "destination": {"name": "Orchard", "lat": 1.3040, "lng": 103.8318},
            "steps": option["steps"], "path": option["path"]}
    route = client.approve_route(client.create_route(RouteIn.model_validate(body).model_dump(by_alias=True))["routeId"])
    trip = client.start_trip(client.create_trip(route["routeId"], "2026-10-22")["tripId"])

    sent = []
    checker = DisruptionChecker(r, client.lta, SETTINGS, clock=lambda: DISRUPTION_NOW)     # what the Lambda runs
    summary = checker.check(sent.append)
    train_step = next(s["stepIndex"] for s in route["steps"] if s["mode"] == "train")
    assert summary["sent"] == 1
    assert (sent[0]["tripId"], sent[0]["travellerId"], sent[0]["affectedStepIndex"]) == (trip["tripId"], "U-042", train_step)
    assert sent[0]["affectedStations"] == ["Serangoon MRT Station", "Woodleigh MRT Station", "Potong Pasir MRT Station"]

    client.end_trip(trip["tripId"])                             # journey over: nothing left to watch
    assert checker.check(sent.append)["activeTrips"] == 0


# =====================================================================================
# Route pack
# =====================================================================================

def test_pack_matches_the_contract_example():
    client, _ = trip_client()
    route = approved_route(client)
    pack = client.build_pack(client.create_trip(route["routeId"], "2026-10-22")["tripId"])
    example_path = Path(__file__).resolve().parents[3] / "contracts" / "examples" / "route-pack.json"
    if example_path.exists():
        example = json.loads(example_path.read_text(encoding="utf-8"))
        assert list(pack) == list(example)
        assert list(pack["steps"][0]) == list(example["steps"][0])
        assert list(pack["messages"]) == list(example["messages"]) and pack["messages"] == example["messages"]
    assert pack["packVersion"] == f"{route['routeId']}-v1" and pack["routeVersion"] == 1
    assert pack["generatedAt"] == "2026-10-22T00:00:00Z" and pack["path"] == route["path"]
    walk, bus = pack["steps"]
    assert (walk["lat"], walk["lng"]) == (1.4052, 103.9024)             # where the step ends
    assert bus["backup"] == "If bus 190 does not come in 20 minutes, take bus 83."
    assert walk["backup"] is None and walk["easyRead"].startswith("Walk to Punggol Interchange")
    assert (pack["travellerName"], pack["caregiver"]) == (None, {"name": None, "phone": None})   # Profiles service not built


# =====================================================================================
# HTTP API
# =====================================================================================

def make_app(settings=SETTINGS, redis=None, authenticator=None):
    redis = redis or fakeredis.FakeRedis()
    return create_app(journey=make(settings=settings, redis=redis), redis_client=redis, s=settings,
                      authenticator=authenticator)


PLAN_BODY = {"from": {"lat": 1.37, "lng": 103.8486}, "to": {"lat": 1.3326, "lng": 103.8474}}


@respx.mock
def test_plan_endpoint():
    mock_onemap("route_pt_bus_mrt.json")
    mock_lta()
    with TestClient(make_app()) as c:
        resp = c.post("/api/routes/plan", json={**PLAN_BODY, "departAt": "2026-10-22T08:00:00"})
    assert resp.status_code == 200
    options = resp.json()["options"]
    assert options and options[0]["steps"][0]["mode"] == "walk"


# --- saved routes over HTTP ---

def test_route_lifecycle_over_http():
    with TestClient(make_app()) as c:
        created = c.post("/api/routes", json=route_data())
        assert created.status_code == 201
        route = created.json()
        rid = route["routeId"]
        assert (route["version"], route["approved"]) == (1, False)

        assert c.get(f"/api/routes/{rid}").json() == route

        edited = c.put(f"/api/routes/{rid}", json={**route_data(name="Home to GP"), "version": 1})
        assert edited.status_code == 200
        assert (edited.json()["version"], edited.json()["name"]) == (2, "Home to GP")

        approved = c.post(f"/api/routes/{rid}/approve")
        assert approved.status_code == 200
        assert (approved.json()["approved"], approved.json()["version"]) == (True, 2)

        listed = c.get("/api/routes", params={"travellerId": "U-042"}).json()["routes"]
        assert [r["routeId"] for r in listed] == [rid] and listed[0]["approved"] is True


def bad(**changes):
    body = route_data()
    for key, value in changes.items():
        if value is ...:
            del body[key]
        else:
            body[key] = value
    return body


# --- trips and packs over HTTP ---


# =====================================================================================
# Who is calling: logins and ownership
# =====================================================================================

POOL = dict(cognito_region="ap-southeast-1", cognito_user_pool_id="ap-southeast-1_TESTPOOL",
            cognito_client_id="client-123")
ISSUER = "https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_TESTPOOL"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)           # the pool's signing key
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)     # an attacker's key


def cognito_settings(**over) -> Settings:
    return Settings(lta_account_key="lta", auth_mode="cognito", **POOL, **over)


def mint(user="G-003", groups=("caregiver",), key=KEY, kid="k1", **claims) -> str:
    """A Cognito-style ID token. A claim set to None is left out."""
    now = int(time.time())
    payload = {"iss": ISSUER, "aud": "client-123", "token_use": "id", "sub": "a1b2", "iat": now, "exp": now + 3600,
               "custom:userId": user, "cognito:groups": list(groups) if groups is not None else None, **claims}
    return jwt.encode({k: v for k, v in payload.items() if v is not None}, key, algorithm="RS256", headers={"kid": kid})


def verifier(**over) -> Authenticator:
    return Authenticator(cognito_settings(**over), key_resolver=lambda token: KEY.public_key())


def bearer(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


def test_valid_caregiver_and_traveller_tokens():
    auth = verifier()
    assert auth.authenticate(bearer(mint("G-003", ["caregiver"]))) == Caller("G-003", "caregiver")
    assert auth.authenticate(bearer(mint("U-042", ["traveller"]))) == Caller("U-042", "traveller")
    assert auth.authenticate(bearer(mint("G-003", ["caregiver", "other-group"]))) == Caller("G-003", "caregiver")
    assert auth.authenticate({"authorization": "bearer " + mint()}) == Caller("G-003", "caregiver")   # scheme is case-insensitive


@pytest.mark.parametrize("why,token", [
    ("expired", mint(exp=int(time.time()) - 60)),
    ("not valid yet", mint(nbf=int(time.time()) + 3600)),
    ("no expiry", mint(exp=None)),
    ("wrong issuer", mint(iss="https://cognito-idp.ap-southeast-1.amazonaws.com/another-pool")),
    ("wrong audience", mint(aud="another-app")),
    ("no audience", mint(aud=None)),
    ("access token where an ID token is expected", mint(token_use="access")),
    ("signed with someone else's key", mint(key=OTHER_KEY)),
    ("no user id", mint(user=None)),
    ("user id in the wrong format", mint("admin")),
    ("user id with extra characters", mint("G-003; DROP TABLE")),
    ("no groups", mint(groups=None)),
    ("groups that mean nothing here", mint(groups=["admins"])),
    ("both roles", mint(groups=["caregiver", "traveller"])),
    ("caregiver id on a traveller account", mint("G-003", ["traveller"])),
    ("traveller id on a caregiver account", mint("U-042", ["caregiver"])),
    ("groups of the wrong type", mint(**{"cognito:groups": 7})),
    ("garbage", "not.a.token"),
])
def test_bad_tokens_are_refused(why, token):
    with pytest.raises(Unauthorized):
        verifier().authenticate(bearer(token))


def test_unsigned_and_algorithm_confusion_tokens_are_refused():
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    payload = b64(json.dumps({"iss": ISSUER, "aud": "client-123", "token_use": "id", "exp": int(time.time()) + 3600,
                              "custom:userId": "G-003", "cognito:groups": ["caregiver"]}).encode())
    unsigned = b64(b'{"alg":"none","typ":"JWT"}') + "." + payload + "."
    header = b64(b'{"alg":"HS256","typ":"JWT"}')
    public_pem = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    hs256 = f"{header}.{payload}.{b64(hmac.new(public_pem, f'{header}.{payload}'.encode(), 'sha256').digest())}"
    for token in (unsigned, hs256):                           # the classic ways to forge a token
        with pytest.raises(Unauthorized):
            verifier().authenticate(bearer(token))


def test_dev_mode_trusts_the_header_and_off_mode_checks_nothing():
    dev = Authenticator(Settings(lta_account_key="lta", auth_mode="dev"))
    assert dev.authenticate({"x-dev-user": "G-1"}) == Caller("G-1", "caregiver")
    assert dev.authenticate({"x-dev-user": "U-1"}) == Caller("U-1", "traveller")
    for bad in ({}, {"x-dev-user": ""}, {"x-dev-user": "admin"}, {"x-dev-user": "G-"}, {"x-dev-user": "X-1"}):
        with pytest.raises(Unauthorized):
            dev.authenticate(bad)
    off = Authenticator(Settings(lta_account_key="lta"))
    assert off.authenticate({}) is None and off.authenticate({"authorization": "garbage"}) is None
    assert not off.enabled and dev.enabled


# --- over HTTP ---

def secured_app(**over):
    return make_app(settings=Settings(lta_account_key="lta", onemap_api_key="om", auth_mode="dev", **over))


def as_user(user: str) -> dict:
    return {"x-dev-user": user}


class World:
    """Caregiver G-1 looks after traveller U-1: an approved route and a planned trip, all made through the API."""
    def __init__(self, c):
        self.c = c
        body = route_data(travellerId="U-1", caregiverId="G-1")
        created = c.post("/api/routes", json=body, headers=as_user("G-1"))
        assert created.status_code == 201, created.text
        self.route_id = created.json()["routeId"]
        assert c.post(f"/api/routes/{self.route_id}/approve", headers=as_user("G-1")).status_code == 200
        trip = c.post("/api/trips", json={"routeId": self.route_id, "date": "2026-10-22"}, headers=as_user("G-1"))
        assert trip.status_code == 201, trip.text
        self.trip_id = trip.json()["tripId"]

    def call(self, user, method, path, **kw):
        return getattr(self.c, method)(path, headers=as_user(user), **kw)

    def route_calls(self, user):
        """Status code of every route and trip action as `user`. Order matters: editing the route comes last,
        because it changes the version and so invalidates the trip for everything after it."""
        r, t = f"/api/routes/{self.route_id}", f"/api/trips/{self.trip_id}"
        edit = {**route_data(travellerId="U-1", caregiverId="G-1", name="edited"), "version": 1}
        return {
            "get route": self.call(user, "get", r).status_code,
            "get trip": self.call(user, "get", t).status_code,
            "pack": self.call(user, "get", f"{t}/pack").status_code,
            "start trip": self.call(user, "post", f"{t}/start").status_code,
            "end trip": self.call(user, "post", f"{t}/end").status_code,
            "new trip": self.call(user, "post", "/api/trips", json={"routeId": self.route_id, "date": "2026-10-23"}).status_code,
            "approve route": self.call(user, "post", f"{r}/approve").status_code,
            "edit route": self.call(user, "put", r, json=edit).status_code,
        }


        # (pack is 503 only because this test app has no S3: the caregiver is allowed, storage is missing)


def test_the_traveller_can_read_and_run_their_own_trip_but_not_change_the_route():
    with TestClient(secured_app()) as c:
        w = World(c)
        assert w.route_calls("U-1") == {"get route": 200, "get trip": 200, "pack": 503, "start trip": 200,
                                        "end trip": 200, "new trip": 403, "approve route": 403, "edit route": 403}
        refused = w.call("U-1", "put", f"/api/routes/{w.route_id}",
                         json={**route_data(travellerId="U-1", caregiverId="G-1"), "version": 1})
        assert refused.json()["error"]["code"] == "forbidden"
        assert w.call("G-1", "get", f"/api/routes/{w.route_id}").json()["version"] == 1       # nothing was changed


@pytest.mark.parametrize("stranger", ["G-2", "U-2"])
def test_a_stranger_gets_not_found_for_everything_and_changes_nothing(stranger):
    with TestClient(secured_app()) as c:
        w = World(c)
        got = w.route_calls(stranger)
        assert set(got.values()) == {404}, got
        assert w.call("G-1", "get", f"/api/routes/{w.route_id}").json()["version"] == 1       # untouched
        assert w.call("G-1", "get", f"/api/routes/{w.route_id}").json()["approved"] is True
        assert w.call("G-1", "get", f"/api/trips/{w.trip_id}").json()["status"] == "planned"


# =====================================================================================
# Saved-data ("recorded") mode: the demo runs from app/demo_data with no keys and no internet
# =====================================================================================

SAVED = Settings(onemap_mode="recorded", lta_mode="recorded", lta_retries=0, onemap_retries=0)   # note: no keys
PUNGGOL, ORCHARD = (1.4043, 103.9021), (1.3040, 103.8318)
AMK, TOA_PAYOH = (1.3700, 103.8486), (1.3326, 103.8474)
DATA = Path(__file__).resolve().parents[1] / "app" / "demo_data"


def saved_client(clock=NOW, redis=None) -> JourneyClient:
    return make(clock=clock, settings=SAVED, redis=redis)


@respx.mock
@pytest.mark.parametrize("origin,destination,mode,expected", [
    (PUNGGOL, ORCHARD, "any", [["walk", "train"]]),
    (PUNGGOL, ORCHARD, "train", [["walk", "train"]]),              # taken out of the saved "any" trip: no bus
    (PUNGGOL, ORCHARD, "bus", [["walk", "bus"]] * 3),
    (AMK, TOA_PAYOH, "bus", [["bus", "walk"]] * 3),
    (AMK, TOA_PAYOH, "train", [["walk", "train"]]),
])
def test_saved_trips_plan_without_any_keys(origin, destination, mode, expected):
    options = saved_client().plan(origin, destination, NOW, mode=mode)["options"]
    assert [o["summary"]["modes"] for o in options] == expected
    assert all(s["easyRead"] for o in options for s in o["steps"])        # the wording is built as usual
    assert respx.calls.call_count == 0


@respx.mock
def test_planning_an_unsaved_trip_answers_404_and_names_the_saved_ones():
    with TestClient(make_app(settings=SAVED)) as c:
        ok = c.post("/api/routes/plan", json={"from": {"lat": PUNGGOL[0], "lng": PUNGGOL[1]},
                                              "to": {"lat": ORCHARD[0], "lng": ORCHARD[1]}, "mode": "train"})
        assert ok.status_code == 200 and ok.json()["options"]
        miss = c.post("/api/routes/plan", json={"from": {"lat": 1.35, "lng": 103.95}, "to": {"lat": 1.29, "lng": 103.78}})
    assert miss.status_code == 404 and miss.json()["error"]["code"] == "not_found"
    assert "Punggol to Orchard" in miss.json()["error"]["message"]


def test_the_saved_data_is_complete_and_holds_no_secrets():
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    places = json.loads((DATA / "places.json").read_text(encoding="utf-8"))
    assert {r["mode"] for r in manifest["routes"]} == {"TRANSIT", "BUS"}
    for route in manifest["routes"]:
        body = json.loads((DATA / route["file"]).read_text(encoding="utf-8"))
        assert body["plan"]["itineraries"] and isinstance(body["plan"]["date"], int)
    for trip in manifest["trips"]:                                   # each preset sits next to the trip it opens
        recorded = next(r for r in manifest["routes"] if r["trip"] == trip["id"])
        assert distance_m((trip["from"]["lat"], trip["from"]["lng"]), tuple(recorded["from"])) <= MATCH_RADIUS_M
        assert distance_m((trip["to"]["lat"], trip["to"]["lng"]), tuple(recorded["to"])) <= MATCH_RADIUS_M
        assert any(trip["from"]["name"].split()[0].lower() in p["SEARCHVAL"].lower() for p in places)
    for code in manifest["lta"]["busArrivalStops"]:
        assert json.loads((DATA / "lta" / f"bus_arrival_{code}.json").read_text(encoding="utf-8"))["Services"]
    for path in DATA.rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "AccountKey" not in text and "eyJ" not in text and "Bearer" not in text, path.name


# =====================================================================================
# Walking limits, how busy a route is, and choosing the order of the options
# =====================================================================================

PUNGGOL_TO_ORCHARD = ((1.4043, 103.9021), (1.3040, 103.8318))        # the MRT trip (route_pt_bus_mrt.json)


def crowd_body(**levels):
    return {"value": [{"Station": s, "StartTime": "2026-10-22T08:00:00+08:00", "EndTime": "2026-10-22T08:10:00+08:00",
                       "CrowdLevel": c} for s, c in levels.items()]}


def mrt_plan(client=None, **kw):
    client = client or make()
    return client.plan(*PUNGGOL_TO_ORCHARD, NOW, **kw)["options"]


@respx.mock
def test_a_trip_that_walks_too_far_in_total_is_flagged_even_if_no_single_walk_is_long():
    mock_onemap("route_pt_bus_short.json")                       # walks 273 m + 42 m = 315 m
    mock_lta()
    flagged = lambda limit: [w for w in make().plan(AMK, TOA_PAYOH, NOW, max_walk_m=limit)["options"][0]["warnings"]
                             if w["type"] == "long_total_walk"]
    assert flagged(150) and flagged(150)[0]["stepIndex"] is None and flagged(150)[0]["distanceM"] == 320
    assert "320 m" in flagged(150)[0]["message"] and "300 m" in flagged(150)[0]["message"]
    assert not flagged(160)                                      # 315 m is not more than twice 160 m


@pytest.mark.parametrize("load,crowd", [("SEA", "low"), ("SDA", "moderate"), ("LSD", "high"), ("", None)])
@respx.mock
def test_a_buses_load_becomes_a_crowd_level(load, crowd):
    respx.get(f"{LTA}/v3/BusArrival").mock(return_value=httpx.Response(200, json={"BusStopCode": "1", "Services": [
        {"ServiceNo": "15", "Operator": "GAS", "NextBus": bus("2024-08-14T16:41:48+08:00", load=load),
         "NextBus2": EMPTY_BUS, "NextBus3": EMPTY_BUS}]}))
    assert make(clock=LTA_NOW).bus_arrival("1")["services"][0]["buses"][0]["crowd"] == crowd


@respx.mock
def test_a_train_leg_shows_how_busy_its_platform_is_and_the_option_takes_the_worst():
    mock_onemap("route_pt_bus_mrt.json")
    mock_lta(crowd=crowd_body(NE17="h", NS24="m"))
    option = mrt_plan()[0]
    trains = [e for e in option["live"] if "station" in e]
    # the second train leaves about half an hour later, so only the first uses the real-time level
    assert [(e["station"], e["crowd"], e["source"]) for e in trains] == [("NE17", "high", "real-time")]
    assert option["summary"]["crowd"] == "high"
    crowded = [w for w in option["warnings"] if w["type"] == "crowded"]
    assert len(crowded) == 1 and crowded[0]["station"] == "NE17" and "very crowded now" in crowded[0]["message"]


@respx.mock
def test_a_later_train_uses_the_forecast_for_its_start_time():
    mock_onemap("route_pt_bus_mrt.json")
    leg = first_leg("route_pt_bus_mrt.json", "SUBWAY")
    start = datetime.fromtimestamp(leg["startTime"] / 1000, tz=timezone.utc).astimezone(SST)
    interval = start.replace(minute=start.minute // 30 * 30, second=0, microsecond=0).isoformat()
    forecast = {"value": [{"Date": interval, "Stations": [
        {"Station": "NE17", "Interval": [{"Start": interval, "CrowdLevel": "h"}]}]}]}
    mock_lta(forecast=forecast, crowd=crowd_body(NE17="l"))                          # real-time says quiet
    option = mrt_plan(make(clock=NOW - timedelta(hours=3)))[0]                       # but the train is hours away
    assert [(e["station"], e["crowd"], e["source"]) for e in option["live"] if "station" in e] == [
        ("NE17", "high", "forecast")]
    assert [w["message"] for w in option["warnings"] if w["type"] == "crowded"] == [
        "Punggol MRT Station is very crowded at this time."]


@respx.mock
def test_a_busy_bus_gets_a_plain_warning():
    leg = first_leg("route_pt_bus_only_short.json", "BUS")
    mock_onemap("route_pt_bus_only_short.json")
    mock_lta(arrival={"Services": [{"ServiceNo": leg["route"], "Operator": "SBST", "NextBus": bus(
        "2026-10-22T08:05:00+08:00", load="LSD")}]})
    option = plan()[0]
    assert option["live"][0]["crowd"] == "high" and option["summary"]["crowd"] == "high"
    assert f"Bus {leg['route']} is very full right now. You may have to stand." in [
        w["message"] for w in option["warnings"] if w["type"] == "crowded"]


@respx.mock
@pytest.mark.parametrize("status", [403, 404, 500])
def test_missing_crowd_data_never_breaks_planning_or_raises_the_live_data_warning(status):
    mock_onemap("route_pt_bus_mrt.json")
    mock_lta()
    respx.get(f"{LTA}/PCDRealTime").mock(return_value=httpx.Response(status, text="nope"))
    option = mrt_plan()[0]
    assert option["steps"] and option["summary"]["crowd"] is None
    assert not [e for e in option["live"] if "station" in e]
    assert not [w for w in option["warnings"] if w["type"] in ("live_data_unavailable", "crowded")]


def ordered_punggol_buses():
    """The saved Punggol -> Orchard bus trip in reverse, so OneMap's order isn't already the sorted one:
    option 1 walks 275 m (43e, 70, 14), options 2 and 3 walk 246 m (62, 683) and (85, 683)."""
    body = onemap_fixture("route_pt_bus_only.json")
    body["plan"]["itineraries"].reverse()
    respx.get(ROUTE).mock(return_value=httpx.Response(200, json=body))


def mock_loads(by_service: dict):
    """Next-bus load per bus service number (everything else is SEA)."""
    def answer(request):
        wanted = request.url.params.get("ServiceNo")
        return httpx.Response(200, json={"Services": [{"ServiceNo": wanted, "Operator": "SBST", "NextBus": bus(
            "2026-10-22T08:05:00+08:00", load=by_service.get(wanted, "SEA"))}]})
    mock_lta()
    respx.get(f"{LTA}/v3/BusArrival").mock(side_effect=answer)


def sorted_by(**kw):
    result = make().plan(*PUNGGOL_TO_ORCHARD, NOW, mode="bus", **kw)
    return result, [o["steps"][1]["line"] for o in result["options"]]            # the first bus of each option


@respx.mock
def test_options_can_be_sorted_by_walking_crowding_or_time():
    ordered_punggol_buses()
    mock_loads({"85": "LSD", "62": "SEA", "43e": "SDA"})
    result, first_buses = sorted_by(sort_by="time")
    assert first_buses == ["43e", "62", "85"] and result["sortedBy"] == "time"      # OneMap's (reversed) order
    result, first_buses = sorted_by(sort_by="walking")
    assert first_buses == ["62", "85", "43e"]                   # 246 m, 246 m (tie keeps OneMap's order), 275 m
    result, first_buses = sorted_by(sort_by="crowding")
    assert first_buses == ["62", "43e", "85"]                   # low, moderate, high
    assert [o["summary"]["crowd"] for o in result["options"]] == ["low", "moderate", "high"]
    assert [o["option"] for o in result["options"]] == [1, 2, 3] and result["sortedBy"] == "crowding"
