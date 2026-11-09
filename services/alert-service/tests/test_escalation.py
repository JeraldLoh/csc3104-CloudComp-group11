"""Escalation (issue 5.4): an urgent alert nobody acknowledges is sent again, once per level, even with replicas."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

import app.main as service
from app.escalation import Escalator
from app.events import CHANNEL
from app.main import app
from tests.conftest import new_caregiver

AFTER = timedelta(seconds=120)


@pytest.fixture
def client(dynamodb):
    return TestClient(app)


class Recorder:
    """Stands in for the publisher and the notifier."""

    def __init__(self):
        self.events, self.pushes = [], []

    def publish(self, event_type, incident):
        self.events.append((event_type, incident["incidentId"], incident["escalationLevel"]))

    def escalated(self, incident):
        self.pushes.append((incident["incidentId"], incident["escalationLevel"]))


class OnlyThese:
    """The incident store, but seeing only this test's travellers: the shared LocalStack keeps other runs' incidents."""

    def __init__(self, travellers):
        self.travellers = set(travellers)

    def open_urgent(self, below_level):
        return [i for i in service.incidents.open_urgent(below_level) if i["travellerId"] in self.travellers]

    def claim_escalation(self, iid, seen_level, now=None):
        return service.incidents.claim_escalation(iid, seen_level, now)


def escalator(traveller, recorder, max_level=3):
    return Escalator(OnlyThese([traveller]), recorder, recorder, after_s=int(AFTER.total_seconds()), max_level=max_level)


def report(client, problem_type="no_response", traveller=None, detected=None):
    traveller = traveller or f"U-{uuid.uuid4().hex[:10]}"
    detected = detected or datetime.now(timezone.utc)
    body = {"reportId": f"p-{uuid.uuid4().hex[:6]}", "source": "heartbeat-monitor", "travellerId": traveller,
            "tripId": "T-001", "stepIndex": 1, "problemType": problem_type, "timestamp": detected.isoformat()}
    iid = client.post("/internal/problems", json=body).json()["incidentId"]
    return traveller, iid


def later(by: timedelta) -> datetime:
    return datetime.now(timezone.utc) + by


def test_not_escalated_before_its_time(client):
    traveller, _ = report(client)
    seen = Recorder()

    assert escalator(traveller, seen).run_once(later(AFTER / 2)) == []
    assert seen.pushes == []


def test_escalated_once_its_time_has_come(client):
    traveller, iid = report(client)
    seen = Recorder()

    escalated = escalator(traveller, seen).run_once(later(AFTER + timedelta(seconds=1)))

    assert [i["incidentId"] for i in escalated] == [iid]
    assert seen.pushes == [(iid, 1)]
    assert seen.events == [("escalated", iid, 1)]
    stored = service.incidents.get(iid)
    assert stored["escalationLevel"] == 1
    assert stored["lastEscalatedAt"]


def test_escalated_again_each_time_up_to_the_limit(client):
    traveller, iid = report(client)
    seen = Recorder()
    e = escalator(traveller, seen, max_level=2)

    for step in range(1, 5):
        e.run_once(later(step * AFTER + timedelta(seconds=step)))

    assert seen.pushes == [(iid, 1), (iid, 2)]
    assert service.incidents.get(iid)["escalationLevel"] == 2


def test_waits_again_after_each_escalation(client):
    traveller, iid = report(client)
    seen = Recorder()
    e = escalator(traveller, seen)

    e.run_once(later(AFTER + timedelta(seconds=1)))  # level 1, sent now
    e.run_once(later(AFTER / 2))  # not long since then

    assert seen.pushes == [(iid, 1)]


def test_acknowledging_stops_it(client):
    me = new_caregiver()
    alex = client.post("/api/travellers", json={"name": "Alex"}, headers={"X-Dev-User": me}).json()["travellerId"]
    _, iid = report(client, traveller=alex)
    client.post(f"/api/incidents/{quote(iid, safe='')}/ack", headers={"X-Dev-User": me})
    seen = Recorder()

    escalator(alex, seen).run_once(later(10 * AFTER))

    assert seen.pushes == []


def test_an_old_alert_is_left_alone(client):
    traveller, _ = report(client)
    seen = Recorder()

    # Two hours on, e.g. a replica coming back after an outage: it's not sent again now.
    escalator(traveller, seen).run_once(later(timedelta(hours=2)))

    assert seen.pushes == []


def test_a_normal_alert_is_never_escalated(client):
    traveller, _ = report(client, problem_type="off_route")
    seen = Recorder()

    escalator(traveller, seen).run_once(later(10 * AFTER))

    assert seen.pushes == []


def test_a_reopened_alert_starts_again_from_level_0(client):
    me = new_caregiver()
    alex = client.post("/api/travellers", json={"name": "Alex"}, headers={"X-Dev-User": me}).json()["travellerId"]
    _, iid = report(client, traveller=alex)
    escalator(alex, Recorder()).run_once(later(AFTER + timedelta(seconds=1)))
    client.post(f"/api/incidents/{quote(iid, safe='')}/resolve", headers={"X-Dev-User": me})

    # The same problem detected again after the resolve (clearly after: times are compared to the second): reopened.
    report(client, traveller=alex, detected=later(timedelta(minutes=1)))

    stored = service.incidents.get(iid)
    assert stored["state"] == "open"
    assert stored["escalationLevel"] == 0
    assert "lastEscalatedAt" not in stored


def test_two_replicas_claiming_the_same_level_send_it_once(client):
    _, iid = report(client)

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: service.incidents.claim_escalation(iid, 0), range(8)))

    assert sum(1 for c in claims if c) == 1
    assert service.incidents.get(iid)["escalationLevel"] == 1


def test_two_replicas_running_at_once_escalate_once(client):
    traveller, iid = report(client)
    when = later(AFTER + timedelta(seconds=1))
    replicas = [(escalator(traveller, r), r) for r in (Recorder() for _ in range(4))]

    with ThreadPoolExecutor(max_workers=4) as pool:  # all checking at the same moment
        list(pool.map(lambda pair: pair[0].run_once(when), replicas))

    assert sum(len(r.pushes) for _, r in replicas) == 1
    assert service.incidents.get(iid)["escalationLevel"] == 1


def test_a_replica_that_saw_it_due_but_lost_the_claim_sends_nothing(client):
    traveller, iid = report(client)
    when = later(AFTER + timedelta(seconds=1))
    first, second = Recorder(), Recorder()
    seen_by_second = escalator(traveller, second).due(when)  # it sees level 0 due...
    escalator(traveller, first).run_once(when)  # ...then the first replica escalates it

    claimed = service.incidents.claim_escalation(iid, seen_by_second[0]["escalationLevel"])  # the second tries

    assert claimed is None
    assert first.pushes == [(iid, 1)]


def test_the_table_scan_finds_open_urgent_alerts_only(client):
    me = new_caregiver()
    alex = client.post("/api/travellers", json={"name": "Alex"}, headers={"X-Dev-User": me}).json()["travellerId"]
    _, urgent = report(client, traveller=alex)
    _, normal = report(client, problem_type="off_route", traveller=alex)
    _, acked = report(client, problem_type="help_requested", traveller=alex)
    client.post(f"/api/incidents/{quote(acked, safe='')}/ack", headers={"X-Dev-User": me})

    found = {i["incidentId"] for i in service.incidents.open_urgent(3)}

    assert urgent in found
    assert normal not in found and acked not in found


def test_the_dashboard_and_caregivers_hear_of_it(client, redis_client, monkeypatch):
    sent = []
    monkeypatch.setattr(service.notifier, "sender", type("S", (), {"send": lambda self, t, d: sent.append(d) or (len(t), [])})())
    monkeypatch.setattr(service.notifier, "run", lambda fn, *args: fn(*args))
    me = new_caregiver()
    alex = client.post("/api/travellers", json={"name": "Alex"}, headers={"X-Dev-User": me}).json()["travellerId"]
    client.post("/api/devices", json={"token": "tok-" + uuid.uuid4().hex * 2}, headers={"X-Dev-User": me})
    _, iid = report(client, traveller=alex)
    sent.clear()
    pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(CHANNEL)
    pubsub.get_message(timeout=1)
    try:
        Escalator(OnlyThese([alex]), service.publisher, service.notifier, after_s=120, max_level=3).run_once(
            later(AFTER + timedelta(seconds=1)))
        events = []
        while (msg := pubsub.get_message(timeout=0.5)) is not None:
            events.append(json.loads(msg["data"]))
    finally:
        pubsub.close()

    mine = [e for e in events if e.get("incident", {}).get("incidentId") == iid]
    assert mine[0]["event"] == "incident.escalated"
    assert mine[0]["incident"]["escalationLevel"] == 1
    assert sent[0]["title"] == "Still not answered: Alex"
    assert sent[0]["body"] == "Alex's phone has stopped responding."
