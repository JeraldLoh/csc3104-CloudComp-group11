"""Shared setup for the journey-service tests: settings, clocks, mocked OneMap/LTA calls and sample data.

OneMap/LTA responses come from app/saved_data/fixtures: real captures made with scripts/capture_data.py
(lta_*.json, onemap_*.json) plus a few hand-built responses for cases that can't be triggered live
(an MRT outage, no buses at night, upstream errors).
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import fakeredis
import httpx
import respx

from app.config import Settings
from app.journey_client import JourneyClient
from app.lta import SST
from app.main import create_app


LTA = "https://datamall2.mytransport.sg/ltaodataservice"
ONEMAP = "https://www.onemap.gov.sg"
ROUTE = f"{ONEMAP}/api/public/routingsvc/route"
SEARCH = f"{ONEMAP}/api/common/elastic/search"
FIXTURES = Path(__file__).resolve().parents[1] / "app" / "saved_data" / "fixtures"

# Live mode: the tests answer the real API addresses with respx (saved data is the service's default).
SETTINGS = Settings(lta_account_key="lta", onemap_api_key="om", onemap_email="", onemap_password="",
                    lta_retries=0, onemap_retries=0, onemap_mode="live", lta_mode="live")
NOW = datetime(2026, 10, 22, 8, 0, tzinfo=SST)                  # planning "now": 8:00 AM tomorrow
LTA_NOW = datetime(2024, 8, 14, 8, 40, tzinfo=timezone.utc)     # 16:40 SST, matches the guide's samples


AMK, TOA_PAYOH = (1.3700, 103.8486), (1.3326, 103.8474)


def onemap_fixture(name: str) -> dict:
    """The response body saved by capture_data.py (it also stores the HTTP status)."""
    return json.loads((FIXTURES / f"onemap_{name}").read_text(encoding="utf-8"))["body"]


def make(clock=NOW, settings=SETTINGS, redis=None) -> JourneyClient:
    return JourneyClient(settings, redis_client=redis, sleep=lambda _: None,
                         clock=lambda: clock.astimezone(timezone.utc))


def mock_onemap(name: str):
    return respx.get(ROUTE).mock(return_value=httpx.Response(200, json=onemap_fixture(name)))


def mock_lta(arrival=None, alerts=None, incidents=None, crowd=None, forecast=None, bus_routes=None, bus_services=None):
    # Where each bus goes and how often (BusRoutes / BusServices): none known unless a test gives some.
    respx.get(f"{LTA}/BusRoutes").mock(return_value=httpx.Response(200, json={"value": bus_routes or []}))
    respx.get(f"{LTA}/BusServices").mock(return_value=httpx.Response(200, json={"value": bus_services or []}))
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


def bus(eta, monitored=1, lat="1.3154918333333334", lng="103.9059125", load="SEA"):
    return {"OriginCode": "77009", "DestinationCode": "77009", "EstimatedArrival": eta,
            "Monitored": monitored, "Latitude": lat, "Longitude": lng, "VisitNumber": "1",
            "Load": load, "Feature": "WAB", "Type": "SD"}


EMPTY_BUS = {k: "" for k in ("OriginCode", "DestinationCode", "EstimatedArrival", "Latitude",
                             "Longitude", "VisitNumber", "Load", "Feature", "Type")} | {"Monitored": 0}


DISRUPTION_NOW = datetime(2026, 10, 21, 2, 5, 12, tzinfo=timezone.utc)

NEL_ALERT = {"value": {
    "Status": 2,
    "AffectedSegments": [{"Line": "NEL", "Direction": "HarbourFront", "Stations": "NE10,NE11,NE12",
                          "FreePublicBus": "", "FreeMRTShuttle": "", "MRTShuttleDirection": ""}],
    "Message": [{"Content": "NEL: no train service between Serangoon and Potong Pasir",
                 "CreatedDate": "2026-10-21 09:33:49"}],     # Singapore time = 01:33:49 UTC
}}


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


def approved_route(client, **over) -> dict:
    route = client.create_route(route_data(**over))
    return client.approve_route(route["routeId"])


def trip_client(clock=NOW):
    r = fakeredis.FakeRedis()
    return make(clock=clock, redis=r), r


def make_app(settings=SETTINGS, redis=None, authenticator=None):
    redis = redis or fakeredis.FakeRedis()
    return create_app(journey=make(settings=settings, redis=redis), redis_client=redis, s=settings,
                      authenticator=authenticator)
