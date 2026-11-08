"""POST /api/arrived (issue 5.5): the traveller pressed "I've arrived"."""
import dataclasses
import json
import uuid
from datetime import datetime, timezone

import pytest
import redis
import redis.asyncio as redis_async
from fastapi.testclient import TestClient

import app.main as service
from app.auth import Caller
from app.config import settings
from app.events import CHANNEL
from app.main import app
from app.stream import event_stream
from tests.conftest import new_caregiver


@pytest.fixture
def client(dynamodb):
    return TestClient(app)


@pytest.fixture
def traveller() -> str:
    return f"U-{uuid.uuid4().hex[:10]}"


@pytest.fixture
def trip() -> str:
    return f"T-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def published(redis_client):
    """Collects what's published while a test runs."""
    pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(CHANNEL)
    pubsub.get_message(timeout=1)

    def read(trip_id: str) -> list[dict]:
        found = []
        while (msg := pubsub.get_message(timeout=0.3)) is not None:
            event = json.loads(msg["data"])
            if event.get("tripId") == trip_id:
                found.append(event)
        return found

    yield read
    pubsub.close()


def test_arriving_with_no_login_tells_caregivers(client, traveller, trip, published):
    resp = client.post("/api/arrived", json={"tripId": trip, "travellerId": traveller})

    assert resp.status_code == 200
    assert resp.json()["tripId"] == trip
    event = published(trip)[0]
    assert event["event"] == "trip.arrived"
    assert event["travellerId"] == traveller
    assert event["arrivedAt"] == resp.json()["arrivedAt"]


def test_arriving_twice_tells_caregivers_once(client, traveller, trip, published):
    body = {"tripId": trip, "travellerId": traveller}

    first = client.post("/api/arrived", json=body).json()
    second = client.post("/api/arrived", json=body).json()

    assert first == second
    assert len(published(trip)) == 1


def test_it_is_not_an_incident(client, traveller, trip):
    client.post("/api/arrived", json={"tripId": trip, "travellerId": traveller})

    assert service.incidents.list_for_traveller(traveller) == []


def test_the_traveller_name_is_used(client, trip, published):
    me = new_caregiver()
    alex = client.post("/api/travellers", json={"name": "Alex"}, headers={"X-Dev-User": me}).json()["travellerId"]

    client.post("/api/arrived", json={"tripId": trip, "travellerId": alex})

    assert published(trip)[0]["travellerName"] == "Alex"


def test_the_trip_context_decides_whose_trip_it_is(client, redis_client, traveller, trip, published):
    redis_client.hset(f"trip:{trip}:context", mapping={"travellerId": traveller})
    redis_client.expire(f"trip:{trip}:context", 60)

    client.post("/api/arrived", json={"tripId": trip, "travellerId": "U-SOMEONEELSE"})

    assert published(trip)[0]["travellerId"] == traveller


def test_an_unknown_trip_with_no_traveller_is_not_found(client, trip):
    assert client.post("/api/arrived", json={"tripId": trip}).status_code == 404


def test_the_phone_is_told_to_retry_if_redis_is_down(client, traveller, trip, monkeypatch):
    def down(*args, **kwargs):
        raise redis.ConnectionError("down")

    monkeypatch.setattr(service.r, "set", down)
    resp = client.post("/api/arrived", json={"tripId": trip, "travellerId": traveller})

    assert resp.status_code == 503  # a 5xx is what makes the traveller app try again


class FakeLogin:
    def __init__(self, caller: Caller):
        self.caller = caller

    def authenticate(self, headers) -> Caller:
        return self.caller


@pytest.mark.parametrize("caller,status", [(Caller("U-SOMEONEELSE", "traveller"), 403), (Caller("G-001", "caregiver"), 403)])
def test_with_cognito_nobody_else_can_say_they_arrived(client, traveller, trip, monkeypatch, caller, status):
    monkeypatch.setattr(service, "settings", dataclasses.replace(service.settings, auth_mode="cognito"))
    monkeypatch.setattr(service, "authenticator", FakeLogin(caller))

    assert client.post("/api/arrived", json={"tripId": trip, "travellerId": traveller}).status_code == status


def test_the_dashboard_sees_the_arrival_even_if_it_was_not_open(client, redis_client, trip):
    me = new_caregiver()
    alex = client.post("/api/travellers", json={"name": "Alex"}, headers={"X-Dev-User": me}).json()["travellerId"]
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    redis_client.hset(f"trip:{trip}:context", mapping={"travellerId": alex, "stepIndex": "3", "status": "active"})
    redis_client.set(f"hb:{trip}:lastseen", now)
    redis_client.lpush(f"trip:{trip}:recent", json.dumps({"seq": 1, "lat": 1.4, "lng": 103.9}))
    for key in (f"trip:{trip}:context", f"hb:{trip}:lastseen", f"trip:{trip}:recent"):
        redis_client.expire(key, 60)
    live = lambda: client.get("/api/travellers/live", headers={"X-Dev-User": me}).json()[alex]  # noqa: E731

    assert live()["arrivedAt"] is None
    arrived_at = client.post("/api/arrived", json={"tripId": trip, "travellerId": alex}).json()["arrivedAt"]
    assert live()["arrivedAt"] == arrived_at


class ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


@pytest.mark.anyio
async def test_stream_only_sends_arrivals_of_my_travellers():
    client = redis_async.Redis.from_url(settings.redis_url, decode_responses=True)
    stream = event_stream(client, ConnectedRequest(), keepalive_s=5, load_allowed=lambda: {"U-MINE"})

    def arrival(traveller_id: str) -> str:
        return json.dumps({"event": "trip.arrived", "travellerId": traveller_id, "tripId": "T-1", "arrivedAt": "x"})

    try:
        await anext(stream)
        await client.publish(CHANNEL, arrival("U-NOTMINE"))
        await client.publish(CHANNEL, arrival("U-MINE"))

        chunk = await anext(stream)
        assert json.loads(chunk[len("data: "):])["travellerId"] == "U-MINE"
    finally:
        await stream.aclose()
        await client.aclose()
