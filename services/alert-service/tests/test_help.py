"""POST /api/help (issue 5.5): the traveller's Help button."""
import dataclasses
import uuid

import pytest
import redis
from fastapi.testclient import TestClient

import app.main as service
from app.auth import Caller
from app.main import app


@pytest.fixture
def client(dynamodb):
    return TestClient(app)


@pytest.fixture
def traveller() -> str:
    return f"U-{uuid.uuid4().hex[:10]}"


def incidents_of(client, traveller_id: str) -> list[dict]:
    """With AUTH_MODE=dev only a linked caregiver may read these, so read the store directly."""
    return service.incidents.list_for_traveller(traveller_id)


def test_help_with_no_login_raises_an_urgent_incident(client, traveller):
    resp = client.post("/api/help", json={"tripId": "T-001", "travellerId": traveller, "stepIndex": 2,
                                          "lat": 1.4052, "lng": 103.9024})

    assert resp.status_code == 200
    assert resp.json() == {"incidentId": f"{traveller}#T-001#help_requested#2"}
    incident = incidents_of(client, traveller)[0]
    assert incident["severity"] == "urgent"
    assert incident["state"] == "open"
    assert incident["source"] == "traveller"
    assert incident["lastLocation"] == {"lat": 1.4052, "lng": 103.9024}


def test_first_step_is_kept_as_step_0(client, traveller):
    resp = client.post("/api/help", json={"tripId": "T-001", "travellerId": traveller, "stepIndex": 0})

    assert resp.json()["incidentId"].endswith("#help_requested#0")


def test_pressing_help_again_is_one_incident(client, traveller):
    body = {"tripId": "T-001", "travellerId": traveller, "stepIndex": 2}

    first = client.post("/api/help", json=body).json()
    second = client.post("/api/help", json=body).json()

    assert first == second
    assert incidents_of(client, traveller)[0]["detectionCount"] == 2


def test_help_with_no_gps_keeps_the_last_location(client, traveller):
    body = {"tripId": "T-001", "travellerId": traveller, "stepIndex": 2}
    client.post("/api/help", json={**body, "lat": 1.4, "lng": 103.9})

    client.post("/api/help", json={**body, "lat": None, "lng": None})

    assert incidents_of(client, traveller)[0]["lastLocation"] == {"lat": 1.4, "lng": 103.9}


def test_help_with_no_gps_at_all_still_raises_the_incident(client, traveller):
    resp = client.post("/api/help", json={"tripId": "T-001", "travellerId": traveller, "lat": None, "lng": None})

    assert resp.status_code == 200
    assert incidents_of(client, traveller)[0]["lastLocation"] is None


def test_a_traveller_id_with_a_hyphen_is_accepted(client):
    traveller = f"U-DEMO-{uuid.uuid4().hex[:8]}"  # the traveller app's session.ts allows these

    assert client.post("/api/help", json={"tripId": "T-001", "travellerId": traveller}).status_code == 200


def test_a_bad_gps_reading_does_not_lose_the_request(client, traveller):
    resp = client.post("/api/help", json={"tripId": "T-001", "travellerId": traveller, "lat": 999, "lng": 103.9})

    assert resp.status_code == 200


def test_an_older_request_gets_the_rest_from_the_trip_context(client, redis_client, traveller):
    trip = f"T-{uuid.uuid4().hex[:8]}"
    redis_client.hset(f"trip:{trip}:context", mapping={"travellerId": traveller, "stepIndex": "3"})

    resp = client.post("/api/help", json={"tripId": trip, "lat": 1.4, "lng": 103.9})

    assert resp.json() == {"incidentId": f"{traveller}#{trip}#help_requested#3"}


def test_the_trip_context_decides_whose_trip_it_is(client, redis_client, traveller):
    trip = f"T-{uuid.uuid4().hex[:8]}"
    redis_client.hset(f"trip:{trip}:context", mapping={"travellerId": traveller, "stepIndex": ""})

    resp = client.post("/api/help", json={"tripId": trip, "travellerId": "U-SOMEONEELSE"})

    assert resp.json()["incidentId"].startswith(f"{traveller}#")


def test_an_unknown_trip_with_no_traveller_is_not_found(client):
    resp = client.post("/api/help", json={"tripId": f"T-{uuid.uuid4().hex[:8]}"})

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_help_still_works_with_redis_down(client, traveller, monkeypatch):
    def down(*args, **kwargs):
        raise redis.ConnectionError("down")

    monkeypatch.setattr(service.r, "hgetall", down)
    monkeypatch.setattr(service.r, "publish", down)

    resp = client.post("/api/help", json={"tripId": "T-001", "travellerId": traveller})

    assert resp.status_code == 200
    assert incidents_of(client, traveller)


class FakeLogin:
    def __init__(self, caller: Caller):
        self.caller = caller

    def authenticate(self, headers) -> Caller:
        return self.caller


@pytest.fixture
def cognito(monkeypatch):
    """Pretends AUTH_MODE=cognito, with `login` as whoever the token says."""
    monkeypatch.setattr(service, "settings", dataclasses.replace(service.settings, auth_mode="cognito"))

    def login(caller: Caller) -> None:
        monkeypatch.setattr(service, "authenticator", FakeLogin(caller))
    return login


def test_with_cognito_a_traveller_can_ask_for_their_own_help(client, traveller, cognito):
    cognito(Caller(traveller, "traveller"))

    assert client.post("/api/help", json={"tripId": "T-001", "travellerId": traveller}).status_code == 200


@pytest.mark.parametrize("caller", [Caller("U-SOMEONEELSE", "traveller"), Caller("G-001", "caregiver")])
def test_with_cognito_nobody_else_can_ask_for_them(client, traveller, cognito, caller):
    cognito(caller)

    assert client.post("/api/help", json={"tripId": "T-001", "travellerId": traveller}).status_code == 403
