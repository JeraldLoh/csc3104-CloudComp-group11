"""Push notifications (issue 5.4): who gets one, and when. Firebase is replaced by a fake sender."""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

import app.main as service
from app.main import app
from app.push import make_sender
from tests.conftest import new_caregiver


class FakeSender:
    def __init__(self, gone=()):
        self.sent: list[tuple[list[str], dict]] = []
        self.gone = set(gone)

    def send(self, tokens, data):
        self.sent.append((tokens, data))
        gone = [t for t in tokens if t in self.gone]
        return len(tokens) - len(gone), gone


@pytest.fixture
def client(dynamodb):
    return TestClient(app)


@pytest.fixture
def push(monkeypatch):
    """Push on, with a fake Firebase, sending straight away instead of on the background thread."""
    sender = FakeSender()
    monkeypatch.setattr(service.notifier, "sender", sender)
    monkeypatch.setattr(service.notifier, "run", lambda fn, *args: fn(*args))
    return sender


def as_(caregiver_id: str) -> dict:
    return {"X-Dev-User": caregiver_id}


def token() -> str:
    return "tok-" + uuid.uuid4().hex * 2


def traveller_with(client, *caregivers: str) -> str:
    alex = client.post("/api/travellers", json={"name": "Alex"}, headers=as_(caregivers[0])).json()["travellerId"]
    for other in caregivers[1:]:
        client.post("/api/links", json={"travellerId": alex, "caregiverId": other}, headers=as_(caregivers[0]))
    return alex


def register(client, caregiver_id: str) -> str:
    t = token()
    resp = client.post("/api/devices", json={"token": t}, headers=as_(caregiver_id))
    assert resp.status_code == 201, resp.text
    return t


def problem(traveller_id: str, problem_type: str = "no_response", step: int = 2) -> dict:
    return {"reportId": f"p-{uuid.uuid4().hex[:6]}", "source": "heartbeat-monitor", "travellerId": traveller_id,
            "tripId": "T-001", "stepIndex": step, "problemType": problem_type, "timestamp": "2026-10-27T08:00:00Z"}


def test_an_urgent_alert_reaches_every_caregiver_of_the_traveller(client, push):
    me, partner, stranger = new_caregiver(), new_caregiver(), new_caregiver()
    alex = traveller_with(client, me, partner)
    mine, theirs = register(client, me), register(client, partner)
    register(client, stranger)

    client.post("/internal/problems", json=problem(alex))

    assert len(push.sent) == 1
    tokens, data = push.sent[0]
    assert sorted(tokens) == sorted([mine, theirs])
    assert data["kind"] == "alert"
    assert data["title"] == "Urgent: Alex"
    assert data["body"] == "Alex's phone has stopped responding."
    assert data["url"] == f"/#dashboard/{alex}"  # a tap opens that traveller
    assert all(isinstance(v, str) for v in data.values())  # Firebase only takes strings


def test_pressing_help_pushes(client, push):
    me = new_caregiver()
    alex = traveller_with(client, me)
    register(client, me)

    client.post("/api/help", json={"tripId": "T-001", "travellerId": alex, "stepIndex": 1})

    assert push.sent[0][1]["body"] == "Alex has asked for help."


@pytest.mark.parametrize("problem_type, level, title", [
    ("no_response", "urgent", "Urgent: Alex"),
    ("off_route", "important", "Check on Alex"),
    ("long_dwell", "important", "Check on Alex"),
    ("tracking_lost", "notice", "About Alex"),
    ("disruption", "notice", "About Alex"),
])
def test_every_level_pushes_once_with_its_own_title(client, push, problem_type, level, title):
    me = new_caregiver()
    alex = traveller_with(client, me)
    register(client, me)

    client.post("/internal/problems", json=problem(alex, problem_type))

    assert len(push.sent) == 1
    assert push.sent[0][1]["title"] == title
    assert push.sent[0][1]["level"] == level


def test_a_repeat_detection_does_not_push_again(client, push):
    me = new_caregiver()
    alex = traveller_with(client, me)
    register(client, me)
    report = problem(alex)

    client.post("/internal/problems", json=report)
    client.post("/internal/problems", json=report)

    assert len(push.sent) == 1


def test_a_reopened_alert_pushes_again(client, push):
    me = new_caregiver()
    alex = traveller_with(client, me)
    register(client, me)
    iid = client.post("/internal/problems", json=problem(alex)).json()["incidentId"]
    client.post(f"/api/incidents/{iid.replace('#', '%23')}/resolve", headers=as_(me))

    client.post("/internal/problems", json={**problem(alex), "timestamp": "2099-01-01T00:00:00Z"})

    assert len(push.sent) == 2


def test_nobody_registered_means_nothing_sent(client, push):
    alex = traveller_with(client, new_caregiver())

    client.post("/internal/problems", json=problem(alex))

    assert push.sent == []


def test_tokens_firebase_says_are_gone_are_forgotten(client, monkeypatch):
    me = new_caregiver()
    alex = traveller_with(client, me)
    dead, alive = register(client, me), register(client, me)
    sender = FakeSender(gone=[dead])
    monkeypatch.setattr(service.notifier, "sender", sender)
    monkeypatch.setattr(service.notifier, "run", lambda fn, *args: fn(*args))

    client.post("/internal/problems", json=problem(alex, step=1))
    client.post("/internal/problems", json=problem(alex, step=2))  # a different problem, so a new incident

    assert sorted(sender.sent[0][0]) == sorted([dead, alive])
    assert sender.sent[1][0] == [alive]


def test_registering_twice_is_one_device(client, push):
    me = new_caregiver()
    alex = traveller_with(client, me)
    t = token()
    for _ in range(2):
        client.post("/api/devices", json={"token": t}, headers=as_(me))

    client.post("/internal/problems", json=problem(alex))

    assert push.sent[0][0] == [t]


def test_a_failing_firebase_never_fails_the_alert(client, push, monkeypatch):
    me = new_caregiver()
    alex = traveller_with(client, me)
    register(client, me)

    def broken(tokens, data):
        raise RuntimeError("Firebase is down")

    monkeypatch.setattr(push, "send", broken)
    resp = client.post("/internal/problems", json=problem(alex))

    assert resp.status_code == 200
    assert resp.json()["created"] is True


def test_push_off_sends_nothing_and_says_so(client, monkeypatch):
    monkeypatch.setattr(service.notifier, "sender", None)
    me = new_caregiver()

    resp = client.post("/api/devices", json={"token": token()}, headers=as_(me))

    assert resp.json() == {"registered": True, "pushEnabled": False}


def test_registering_needs_a_caregiver(client):
    assert client.post("/api/devices", json={"token": token()}).status_code == 401
    assert client.post("/api/devices", json={"token": token()}, headers=as_("U-42")).status_code == 403


# ---------------------------------------------------------------------------- the traveller's phone
def test_where_are_you_reaches_the_travellers_phone(client, push):
    me = new_caregiver()
    alex = traveller_with(client, me)
    phone = token()
    assert client.post("/api/devices/traveller", json={"travellerId": alex, "token": phone}).status_code == 201

    question = client.post(f"/api/travellers/{alex}/where-are-you", headers=as_(me)).json()
    client.post(f"/api/travellers/{alex}/where-are-you", headers=as_(me))  # asked again while it waits

    assert len(push.sent) == 1
    tokens, data = push.sent[0]
    assert tokens == [phone]
    assert data == {"kind": "where", "travellerId": alex, "requestId": question["requestId"],
                    "title": "Where are you?", "body": "Your caregiver is asking. Tap to answer."}


def test_a_traveller_phone_does_not_get_caregiver_alerts(client, push):
    me = new_caregiver()
    alex = traveller_with(client, me)
    client.post("/api/devices/traveller", json={"travellerId": alex, "token": token()})

    client.post("/internal/problems", json=problem(alex))

    assert push.sent == []


# ---------------------------------------------------------------------------- starting up
def test_no_key_means_push_off(tmp_path):
    assert make_sender("") is None
    assert make_sender(str(tmp_path / "missing.json")) is None


def test_a_broken_key_means_push_off_not_a_crash(tmp_path):
    bad = tmp_path / "key.json"
    bad.write_text(json.dumps({"type": "not_a_service_account"}))

    assert make_sender(str(bad)) is None
