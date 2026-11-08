"""GET /api/travellers/live (issue 5.5): where a caregiver's travellers are, from the Check-in Service's Redis keys."""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import redis
from fastapi.testclient import TestClient

import app.main as service
from app.config import settings
from app.main import app
from tests.conftest import new_caregiver


@pytest.fixture
def client(dynamodb):
    return TestClient(app)


def as_(caregiver_id: str) -> dict:
    return {"X-Dev-User": caregiver_id}


def add_traveller(client, caregiver_id: str) -> str:
    return client.post("/api/travellers", json={"name": "Alex"}, headers=as_(caregiver_id)).json()["travellerId"]


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def check_in(redis_client, traveller_id: str, points: list[tuple[float, float]], step: str = "2",
             seen: datetime | None = None) -> str:
    """Leaves what the Check-in Service would for a trip: `points` oldest first."""
    trip = f"T-{uuid.uuid4().hex[:8]}"
    redis_client.hset(f"trip:{trip}:context", mapping={"travellerId": traveller_id, "stepIndex": step,
                                                       "routeVersion": "1", "status": "active"})
    redis_client.set(f"hb:{trip}:lastseen", iso(seen or datetime.now(timezone.utc)))
    for seq, (lat, lng) in enumerate(points):
        redis_client.lpush(f"trip:{trip}:recent", json.dumps({"seq": seq, "lat": lat, "lng": lng, "accuracyM": 10,
                                                              "batteryPct": 64, "status": "active"}))
    for key in (f"trip:{trip}:context", f"hb:{trip}:lastseen", f"trip:{trip}:recent"):
        redis_client.expire(key, 60)
    return trip


def live_for(client, caregiver_id: str) -> dict:
    resp = client.get("/api/travellers/live", headers=as_(caregiver_id))
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_a_travellers_step_and_location(client, redis_client):
    me = new_caregiver()
    alex = add_traveller(client, me)
    trip = check_in(redis_client, alex, [(1.40, 103.90), (1.41, 103.91)], step="2")

    found = live_for(client, me)[alex]

    assert found["tripId"] == trip
    assert found["stepIndex"] == 2
    assert (found["lat"], found["lng"]) == (1.41, 103.91)
    assert found["batteryPct"] == 64
    assert found["status"] == "active"
    assert found["trail"] == [[1.40, 103.90], [1.41, 103.91]]


def test_step_0_is_not_mistaken_for_unknown(client, redis_client):
    me = new_caregiver()
    alex = add_traveller(client, me)
    check_in(redis_client, alex, [(1.4, 103.9)], step="0")

    assert live_for(client, me)[alex]["stepIndex"] == 0


def test_an_unknown_step_is_null(client, redis_client):
    me = new_caregiver()
    alex = add_traveller(client, me)
    check_in(redis_client, alex, [(1.4, 103.9)], step="")

    assert live_for(client, me)[alex]["stepIndex"] is None


def test_a_traveller_with_no_trip_is_left_out(client):
    me = new_caregiver()
    add_traveller(client, me)

    assert live_for(client, me) == {}


def test_only_my_own_travellers(client, redis_client):
    me, stranger = new_caregiver(), new_caregiver()
    alex = add_traveller(client, me)
    add_traveller(client, stranger)
    check_in(redis_client, alex, [(1.4, 103.9)])

    assert list(live_for(client, me)) == [alex]
    assert alex not in live_for(client, stranger)


def test_a_linked_caregiver_sees_a_trip_someone_else_planned(client, redis_client):
    me, partner = new_caregiver(), new_caregiver()
    alex = add_traveller(client, me)
    client.post("/api/links", json={"travellerId": alex, "caregiverId": partner}, headers=as_(me))
    check_in(redis_client, alex, [(1.4, 103.9)])

    assert alex in live_for(client, partner)


def test_the_latest_trip_wins(client, redis_client):
    me = new_caregiver()
    alex = add_traveller(client, me)
    now = datetime.now(timezone.utc)
    check_in(redis_client, alex, [(1.1, 103.1)], seen=now - timedelta(minutes=5))
    latest = check_in(redis_client, alex, [(1.2, 103.2)], seen=now)
    check_in(redis_client, alex, [(1.3, 103.3)], seen=now - timedelta(minutes=2))

    assert live_for(client, me)[alex]["tripId"] == latest


def test_a_trip_gone_quiet_for_too_long_is_left_out(client, redis_client):
    me = new_caregiver()
    alex = add_traveller(client, me)
    check_in(redis_client, alex, [(1.4, 103.9)], seen=datetime.now(timezone.utc) - timedelta(seconds=settings.live_window_s + 60))

    assert live_for(client, me) == {}


def test_a_phone_quiet_for_a_while_is_still_shown(client, redis_client):
    me = new_caregiver()
    alex = add_traveller(client, me)
    seen = datetime.now(timezone.utc) - timedelta(minutes=10)
    check_in(redis_client, alex, [(1.4, 103.9)], seen=seen)

    assert live_for(client, me)[alex]["lastSeenAt"] == iso(seen)


def test_needs_a_caregiver_login(client):
    assert client.get("/api/travellers/live").status_code == 401
    assert client.get("/api/travellers/live", headers=as_("U-42")).status_code == 403


def test_redis_down_is_a_clear_error(client, monkeypatch):
    me = new_caregiver()
    add_traveller(client, me)

    def down(*args, **kwargs):
        raise redis.ConnectionError("down")

    monkeypatch.setattr(service.r, "scan_iter", down)
    resp = client.get("/api/travellers/live", headers=as_(me))

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "unavailable"
