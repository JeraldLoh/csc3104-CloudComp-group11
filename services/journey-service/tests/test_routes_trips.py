"""Saved routes, trips and the route pack, against the Redis stand-in."""
import json
import re
from pathlib import Path

import pytest
import respx

from app.disruption import DisruptionChecker
from app.models import RouteIn
from app.route_store import RouteConflict, RouteNotFound
from app.trip_store import TripConflict
from helpers import (DISRUPTION_NOW, NEL_ALERT, SETTINGS, approved_route, mock_lta, mock_onemap, plan, route_data,
                     routes_client, trip_client)


ROUTE_ID_FORMAT = re.compile(r"^R-[0-9A-F]{6}$")


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


def test_delete_removes_the_route_from_get_and_the_traveller_list():
    client = routes_client()
    keep, gone = client.create_route(route_data()), client.create_route(route_data(name="Other"))
    client.delete_route(gone["routeId"], expected_version=1)
    with pytest.raises(RouteNotFound):
        client.get_route(gone["routeId"])
    assert [r["routeId"] for r in client.list_routes("U-042")] == [keep["routeId"]]
    with pytest.raises(RouteNotFound):
        client.delete_route(gone["routeId"])                       # already gone


def test_delete_with_a_stale_version_is_refused_and_keeps_the_route():
    client = routes_client()
    saved = client.create_route(route_data())
    client.update_route(saved["routeId"], route_data(name="Edited"), expected_version=1)
    with pytest.raises(RouteConflict) as e:
        client.delete_route(saved["routeId"], expected_version=1)
    assert e.value.current_version == 2
    assert client.get_route(saved["routeId"])["name"] == "Edited"


def test_a_route_with_a_trip_under_way_cannot_be_deleted_but_planned_trips_just_stop_working():
    client, _ = trip_client()
    route = approved_route(client)
    planned = client.create_trip(route["routeId"], "2026-10-22")
    active = client.create_trip(route["routeId"], "2026-10-22")
    client.start_trip(active["tripId"])
    with pytest.raises(TripConflict, match="under way"):
        client.delete_route(route["routeId"])
    client.end_trip(active["tripId"])
    client.delete_route(route["routeId"])
    with pytest.raises(TripConflict, match="deleted"):             # a clear reason, not "not found"
        client.start_trip(planned["tripId"])


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
