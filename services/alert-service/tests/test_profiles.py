"""Profiles (issue 5.1): travellers, caregiver links, safe locations, and "a caregiver only sees their own
travellers" across every endpoint, including the live stream."""
import json
from urllib.parse import quote

import pytest
import redis.asyncio as redis_async
from fastapi.testclient import TestClient

from app.config import settings
from app.events import CHANNEL, build_event
from app.main import app, profiles
from app.stream import event_stream
from tests.conftest import new_caregiver


@pytest.fixture
def client(dynamodb):
    return TestClient(app)


def as_(caregiver_id: str) -> dict:
    return {"X-Dev-User": caregiver_id}


def add_traveller(client, caregiver_id: str, name: str = "Alex") -> dict:
    resp = client.post("/api/travellers", json={"name": name, "preferences": {"voice": True}}, headers=as_(caregiver_id))
    assert resp.status_code == 201, resp.text
    return resp.json()


def problem_for(traveller_id: str) -> dict:
    return {"reportId": "p-1", "source": "aggregator", "travellerId": traveller_id, "tripId": "T-001",
            "stepIndex": 2, "problemType": "off_route", "timestamp": "2026-10-27T08:00:00Z"}


# ---------------------------------------------------------------------------- travellers and links
def test_adding_a_traveller_links_them_to_me(client):
    me = new_caregiver()
    alex = add_traveller(client, me, "Alex")

    assert alex["travellerId"].startswith("U-")
    assert alex["name"] == "Alex"
    assert alex["preferences"] == {"voice": True}
    mine = client.get("/api/travellers", headers=as_(me)).json()
    assert [t["travellerId"] for t in mine] == [alex["travellerId"]]


def test_my_travellers_are_listed_by_name(client):
    me = new_caregiver()
    add_traveller(client, me, "Zoe")
    add_traveller(client, me, "alex")

    assert [t["name"] for t in client.get("/api/travellers", headers=as_(me)).json()] == ["alex", "Zoe"]


def test_a_linked_caregiver_can_add_another_caregiver(client):
    me, partner = new_caregiver(), new_caregiver()
    alex = add_traveller(client, me)

    resp = client.post("/api/links", json={"travellerId": alex["travellerId"], "caregiverId": partner}, headers=as_(me))

    assert resp.status_code == 201
    assert resp.json()["linkedBy"] == me
    assert [t["travellerId"] for t in client.get("/api/travellers", headers=as_(partner)).json()] == [alex["travellerId"]]


def test_linking_twice_keeps_the_first_link(client):
    me, partner = new_caregiver(), new_caregiver()
    alex = add_traveller(client, me)
    body = {"travellerId": alex["travellerId"], "caregiverId": partner}

    first = client.post("/api/links", json=body, headers=as_(me)).json()
    second = client.post("/api/links", json=body, headers=as_(me)).json()

    assert second["linkedAt"] == first["linkedAt"]


def test_a_stranger_cannot_link_themselves_to_my_traveller(client):
    me, stranger = new_caregiver(), new_caregiver()
    alex = add_traveller(client, me)

    resp = client.post("/api/links", json={"travellerId": alex["travellerId"], "caregiverId": stranger},
                       headers=as_(stranger))

    assert resp.status_code == 404
    assert client.get("/api/travellers", headers=as_(stranger)).json() == []


def test_a_linked_caregiver_sees_who_else_looks_after_a_traveller(client):
    me, partner = new_caregiver(), new_caregiver()
    alex = add_traveller(client, me)
    client.post("/api/links", json={"travellerId": alex["travellerId"], "caregiverId": partner}, headers=as_(me))

    resp = client.get(f"/api/travellers/{alex['travellerId']}/caregivers", headers=as_(partner))

    assert resp.status_code == 200
    assert resp.json() == sorted([me, partner])


def test_a_stranger_or_the_traveller_cannot_list_caregivers(client):
    me, stranger = new_caregiver(), new_caregiver()
    alex = add_traveller(client, me)
    path = f"/api/travellers/{alex['travellerId']}/caregivers"

    assert client.get(path, headers=as_(stranger)).status_code == 404
    assert client.get(path, headers=as_(alex["travellerId"])).status_code == 404


# ---------------------------------------------------------------------------- removing a traveller
def test_removing_my_only_traveller_deletes_their_profile(client):
    me = new_caregiver()
    alex = add_traveller(client, me)
    client.post(f"/api/travellers/{alex['travellerId']}/safe-locations", json={"name": "Home", "lat": 1.4, "lng": 103.9},
                headers=as_(me))
    client.post("/internal/problems", json=problem_for(alex["travellerId"]))

    resp = client.delete(f"/api/travellers/{alex['travellerId']}", headers=as_(me))

    assert resp.status_code == 200
    assert resp.json() == {"travellerId": alex["travellerId"], "removed": True, "profileDeleted": True}
    assert client.get("/api/travellers", headers=as_(me)).json() == []
    assert client.get("/api/incidents", params={"travellerId": alex["travellerId"]}, headers=as_(me)).status_code == 404
    # the profile and safe locations are gone from the tables
    assert profiles.get_traveller(alex["travellerId"]) is None
    assert profiles.safe_locations(alex["travellerId"]) == []


def test_removing_a_shared_traveller_keeps_them_for_the_other_caregiver(client):
    me, partner = new_caregiver(), new_caregiver()
    alex = add_traveller(client, me)
    client.post("/api/links", json={"travellerId": alex["travellerId"], "caregiverId": partner}, headers=as_(me))

    resp = client.delete(f"/api/travellers/{alex['travellerId']}", headers=as_(me))

    assert resp.json()["profileDeleted"] is False
    assert client.get("/api/travellers", headers=as_(me)).json() == []
    assert [t["name"] for t in client.get("/api/travellers", headers=as_(partner)).json()] == ["Alex"]


def test_a_stranger_cannot_remove_my_traveller(client):
    me, stranger = new_caregiver(), new_caregiver()
    alex = add_traveller(client, me)

    assert client.delete(f"/api/travellers/{alex['travellerId']}", headers=as_(stranger)).status_code == 404
    assert [t["name"] for t in client.get("/api/travellers", headers=as_(me)).json()] == ["Alex"]


# ---------------------------------------------------------------------------- only my own travellers
def test_a_stranger_cannot_see_my_travellers_alerts(client):
    me, stranger = new_caregiver(), new_caregiver()
    alex = add_traveller(client, me)
    iid = client.post("/internal/problems", json=problem_for(alex["travellerId"])).json()["incidentId"]

    assert client.get("/api/incidents", params={"travellerId": alex["travellerId"]}, headers=as_(me)).status_code == 200
    for resp in (
        client.get("/api/incidents", params={"travellerId": alex["travellerId"]}, headers=as_(stranger)),
        client.post(f"/api/incidents/{quote(iid, safe='')}/ack", headers=as_(stranger)),
        client.post(f"/api/incidents/{quote(iid, safe='')}/resolve", headers=as_(stranger)),
    ):
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"
    # ...and the stranger's attempts changed nothing
    assert client.get("/api/incidents", params={"travellerId": alex["travellerId"]},
                      headers=as_(me)).json()[0]["state"] == "open"


def test_a_stranger_cannot_see_or_add_safe_locations(client):
    me, stranger = new_caregiver(), new_caregiver()
    alex = add_traveller(client, me)
    path = f"/api/travellers/{alex['travellerId']}/safe-locations"

    assert client.get(path, headers=as_(stranger)).status_code == 404
    assert client.post(path, json={"name": "Home", "lat": 1.4, "lng": 103.9}, headers=as_(stranger)).status_code == 404


def test_profiles_need_a_caregiver_login(client):
    no_login = client.get("/api/travellers")
    traveller = client.post("/api/travellers", json={"name": "Alex"}, headers=as_("U-42"))

    assert no_login.status_code == 401
    assert traveller.status_code == 403
    assert traveller.json()["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------- safe locations
def test_add_and_list_safe_locations(client):
    me = new_caregiver()
    alex = add_traveller(client, me)
    path = f"/api/travellers/{alex['travellerId']}/safe-locations"

    home = client.post(path, json={"name": "Home", "lat": 1.4043, "lng": 103.9021}, headers=as_(me))
    client.post(path, json={"name": "Day centre", "lat": 1.304, "lng": 103.8318}, headers=as_(me))

    assert home.status_code == 201
    assert home.json()["locationId"].startswith("L-")
    listed = client.get(path, headers=as_(me)).json()
    assert [loc["name"] for loc in listed] == ["Day centre", "Home"]
    assert listed[1]["lat"] == 1.4043


def test_a_traveller_can_read_their_own_safe_locations(client):
    me = new_caregiver()
    alex = add_traveller(client, me)
    path = f"/api/travellers/{alex['travellerId']}/safe-locations"
    client.post(path, json={"name": "Home", "lat": 1.4, "lng": 103.9}, headers=as_(me))

    assert [loc["name"] for loc in client.get(path, headers=as_(alex["travellerId"])).json()] == ["Home"]


def test_a_safe_location_must_be_a_real_place(client):
    me = new_caregiver()
    alex = add_traveller(client, me)

    resp = client.post(f"/api/travellers/{alex['travellerId']}/safe-locations",
                       json={"name": "Nowhere", "lat": 123, "lng": 103.9}, headers=as_(me))

    assert resp.status_code == 422


# ---------------------------------------------------------------------------- for other services
def test_internal_link_check(client):
    me, stranger = new_caregiver(), new_caregiver()
    alex = add_traveller(client, me)

    def linked(caregiver_id):
        return client.get("/internal/links", params={"caregiverId": caregiver_id, "travellerId": alex["travellerId"]}).json()

    assert linked(me) == {"linked": True}
    assert linked(stranger) == {"linked": False}


def test_alerts_use_the_travellers_name(client, redis_client):
    me = new_caregiver()
    alex = add_traveller(client, me, "Alex")
    pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(CHANNEL)
    pubsub.get_message(timeout=1)
    try:
        client.post("/internal/problems", json=problem_for(alex["travellerId"]))
        for _ in range(20):
            msg = pubsub.get_message(timeout=0.5)
            if msg and json.loads(msg["data"])["incident"]["travellerId"] == alex["travellerId"]:
                event = json.loads(msg["data"])
                break
        else:
            raise AssertionError("no event")
    finally:
        pubsub.close()

    assert event["incident"]["travellerName"] == "Alex"
    assert event["incident"]["message"] == "Alex may have gone off route."


# ---------------------------------------------------------------------------- the live stream
class ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


def event_for(traveller_id: str) -> str:
    incident = {"incidentId": f"{traveller_id}#T-001#off_route#2", "travellerId": traveller_id,
                "problemType": "off_route", "severity": "normal", "state": "open", "createdAt": "2026-10-27T08:00:00Z"}
    return json.dumps(build_event("opened", incident))


@pytest.mark.anyio
async def test_stream_only_sends_alerts_for_my_travellers():
    client = redis_async.Redis.from_url(settings.redis_url, decode_responses=True)
    stream = event_stream(client, ConnectedRequest(), keepalive_s=5, load_allowed=lambda: {"U-MINE"})
    try:
        await anext(stream)
        await client.publish(CHANNEL, event_for("U-NOTMINE"))
        await client.publish(CHANNEL, event_for("U-MINE"))

        chunk = await anext(stream)
        assert json.loads(chunk[len("data: "):])["incident"]["travellerId"] == "U-MINE"
    finally:
        await stream.aclose()
        await client.aclose()


def test_stream_refuses_a_traveller_login(client):
    resp = client.get("/api/stream", params={"devUser": "U-42"})

    assert resp.status_code == 403
