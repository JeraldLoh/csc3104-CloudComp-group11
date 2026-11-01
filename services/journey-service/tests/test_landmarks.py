"""Landmarks along a route (landmarks.py): where to look, what is there, and never near home."""
import dataclasses
import json

import fakeredis
import httpx
import respx

from app.easy_read_ai import ai_request, suggest
from app.journey_client import JourneyClient
from app.landmarks import kind, offer, side, spots
from helpers import ONEMAP, SETTINGS

M = 0.0008983                  # 100 m in degrees, near the equator
LAT, LNG = 1.35, 103.94
REVGEOCODE = f"{ONEMAP}/api/public/revgeocode"
EASYREAD = "http://easyread.test:8000"


def east(m: float) -> tuple[float, float]:
    return LAT, LNG + m / 100 * M


def point(m: float, name: str | None = None, code: str | None = None) -> dict:
    lat, lng = east(m)
    return {"name": name, "code": code, "lat": lat, "lng": lng}


# Home -> walk 500 m east -> bus 1.5 km east -> walk 300 m east to Grandma's flat, all along one straight road.
ROUTE = {
    "origin": point(0, "Home"), "destination": point(2300, "Grandma's flat"),
    "path": [list(east(m)) for m in range(0, 2301, 100)],
    "steps": [
        {"stepIndex": 0, "mode": "walk", "from": point(0, "Home"), "to": point(500, "Opp Blk 1", "11111")},
        {"stepIndex": 1, "mode": "bus", "line": "190", "stopCount": 4,
         "from": point(500, "Opp Blk 1", "11111"), "to": point(2000, "Bedok Int", "22222")},
        {"stepIndex": 2, "mode": "walk", "from": point(2000, "Bedok Int", "22222"), "to": point(2300, "Grandma's flat")},
    ],
}


def test_spots_along_each_step_but_never_near_home():
    found = spots(ROUTE)
    assert {i: sorted(s) for i, s in found.items()} == {
        0: ["end", "middle"],             # its start is home
        1: ["before_stop", "end"],
        2: ["start"],                     # its middle (150 m) and end are too close to Grandma's flat
    }
    assert abs(found[1]["before_stop"].lng - east(1700)[1]) < 1e-6         # 300 m before getting off


def test_train_steps_and_changes_of_line_get_no_spots():
    route = {**ROUTE, "steps": [
        {"stepIndex": 0, "mode": "train", "from": point(0, "Bedok", "EW5"), "to": point(1000, "Tanah Merah", "EW4")},
        {"stepIndex": 1, "mode": "walk", "from": point(1000, "Tanah Merah", "EW4"), "to": point(1100, "Tanah Merah", "CG")},
    ]}
    assert spots(route) == {}


def test_side_of_the_road():
    going_east = (east(0), east(100))
    assert side(going_east, (LAT + 0.0003, east(50)[1])) == "left"
    assert side(going_east, (LAT - 0.0003, east(50)[1])) == "right"
    assert side(None, east(50)) is None


def test_kind():
    assert [kind(n) for n in ("Tampines Mall", "Bedok MRT Station", "Bedok Residences", "Cpf Building")] == [
        "mall", "station", "condominium", "building"]
    # Not parks: a car park, and estates called gardens. Real parks still are.
    assert [kind(n) for n in ("Multi Storey Car Park", "Lew Lian Gardens", "Beauty Garden", "Park Place Residences",
                              "Bishan Park", "Gardens By The Bay")] == [
        "building", "condominium", "condominium", "condominium", "park", "park"]


def test_offer_keeps_what_is_on_the_way_the_obvious_first():
    def nearby(lat, lng):
        return [
            {"name": "Block 12", "lat": lat - 0.0003, "lng": lng},               # 33 m south: right
            {"name": "Bedok Mall", "lat": lat + 0.0004, "lng": lng},             # 44 m north: left
            {"name": "Far Tower", "lat": lat + 0.0015, "lng": lng},              # 167 m off the road
            {"name": "Bedok Int", "lat": lat, "lng": lng},                       # the stop itself
        ]
    found = offer(ROUTE, spots(ROUTE), nearby)
    end = found[1]["end"]
    assert [(b["name"], b["kind"], b["side"], b["metres"]) for b in end] == [
        ("Bedok Mall", "mall", "left", 40), ("Block 12", "building", "right", 30)]
    assert end[0]["seenFrom"] == [round(c, 6) for c in east(2000)]           # the stop, where it is seen from
    ids = [b["id"] for by_spot in found.values() for spot in by_spot.values() for b in spot]
    assert len(ids) == len(set(ids)) == 12          # numbered across the route (step 0 also sees Bedok Int)


def test_request_sends_landmarks_without_coordinates():
    found = {1: {"end": [{"id": "L1", "name": "Bedok Mall", "kind": "mall", "metres": 40, "side": "left",
                          "lat": 1.3, "lng": 103.9}]}}
    body, _ = ai_request(ROUTE["steps"], found)
    assert body["steps"][1]["spots"] == {"end": [{"id": "L1", "name": "Bedok Mall", "kind": "mall", "metres": 40,
                                                  "side": "left"}]}
    assert "spots" not in body["steps"][0] and "103.9" not in json.dumps(body)


@respx.mock
def test_suggest_returns_the_landmarks_of_that_step_only():
    found = {0: {"end": [{"id": "L1", "name": "Bedok Mall", "kind": "mall", "metres": 40, "side": "left",
                          "lat": 1.3, "lng": 103.9}]},
             1: {"end": [{"id": "L2", "name": "Tampines Mall", "kind": "mall", "metres": 20, "side": None,
                          "lat": 1.4, "lng": 103.95}]}}
    respx.post(f"{EASYREAD}/internal/easyread").mock(return_value=httpx.Response(200, json={
        "provider": "ollama", "steps": [{"stepIndex": 0, "easyRead": "Walk.", "fallback": False, "landmarks": ["L1"]},
                                        {"stepIndex": 1, "easyRead": "Ride.", "fallback": False, "landmarks": ["L1"]}]}))
    first, second = suggest(ROUTE["steps"][:2], httpx.Client(), EASYREAD, 5, found)["steps"]
    assert first["landmarks"] == [{"name": "Bedok Mall", "kind": "mall", "lat": 1.3, "lng": 103.9, "seenFrom": None}]
    assert second["landmarks"] == []                                            # L1 wasn't offered for step 1


@respx.mock
def test_nearby_buildings_from_onemap_cleaned_and_cached():
    look = respx.get(REVGEOCODE).mock(return_value=httpx.Response(200, json={"GeocodeInfo": [
        {"BUILDINGNAME": "NIL", "LATITUDE": "1.35", "LONGITUDE": "103.94"},
        {"BUILDINGNAME": "HAWKER CENTRE (BLK 208B NEW UPPER CHANGI ROAD)", "LATITUDE": "1.3501", "LONGITUDE": "103.94"},
        {"BUILDINGNAME": "BEDOK MRT STATION", "LATITUDE": "1.3502", "LONGITUDE": "103.9401"}]}))
    j = JourneyClient(SETTINGS, redis_client=fakeredis.FakeRedis())
    expected = [{"name": "Hawker Centre", "lat": 1.3501, "lng": 103.94},
                {"name": "Bedok MRT Station", "lat": 1.3502, "lng": 103.9401}]
    assert j.nearby_buildings(1.35, 103.94) == expected
    assert j.nearby_buildings(1.35001, 103.94001) == expected                  # same ~10 m square: cached
    assert look.call_count == 1
    assert look.calls[0].request.headers["Authorization"] == "Bearer om"


@respx.mock
def test_nearby_buildings_failing_means_none():
    respx.get(REVGEOCODE).mock(side_effect=httpx.ConnectError("down"))
    assert JourneyClient(SETTINGS, redis_client=fakeredis.FakeRedis()).nearby_buildings(1.35, 103.94) == []
    no_token = dataclasses.replace(SETTINGS, onemap_api_key="")
    assert JourneyClient(no_token, redis_client=fakeredis.FakeRedis()).nearby_buildings(1.35, 103.94) == []
