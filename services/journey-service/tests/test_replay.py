"""Saved-data ("recorded") mode: the demo runs from app/saved_data with no keys and no internet."""
import json
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.journey_client import JourneyClient
from app.replay import MATCH_RADIUS_M, distance_m
from helpers import AMK, NOW, TOA_PAYOH, make, make_app


SAVED = Settings(onemap_mode="recorded", lta_mode="recorded", lta_retries=0, onemap_retries=0)   # note: no keys
PUNGGOL, ORCHARD = (1.4043, 103.9021), (1.3040, 103.8318)
DATA = Path(__file__).resolve().parents[1] / "app" / "saved_data"


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
