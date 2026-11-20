""""Where are you?" (issue 5.5): a caregiver asks, the traveller's phone picks it up and replies."""
import dataclasses
import json

import pytest
import redis
from fastapi.testclient import TestClient

import app.main as service
from app.auth import Caller
from app.events import CHANNEL
from app.main import app
from tests.conftest import new_caregiver


@pytest.fixture
def client(dynamodb):
    return TestClient(app)


def as_(caregiver_id: str) -> dict:
    return {"X-Dev-User": caregiver_id}


@pytest.fixture
def setup(client):
    """A caregiver and their traveller."""
    me = new_caregiver()
    alex = client.post("/api/travellers", json={"name": "Alex"}, headers=as_(me)).json()["travellerId"]
    return me, alex


@pytest.fixture
def published(redis_client):
    pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(CHANNEL)
    pubsub.get_message(timeout=1)

    def read(traveller_id: str) -> list[dict]:
        found = []
        while (msg := pubsub.get_message(timeout=0.3)) is not None:
            event = json.loads(msg["data"])
            if event.get("travellerId") == traveller_id and event["event"].startswith("where."):
                found.append(event)
        return found

    yield read
    pubsub.close()


def ask(client, me, alex, trip=None):
    body = {"tripId": trip} if trip else None
    return client.post(f"/api/travellers/{alex}/where-are-you", headers=as_(me), json=body)


def phone_sees(client, alex, trip=None):
    params = {"travellerId": alex, **({"tripId": trip} if trip else {})}
    resp = client.get("/api/where-are-you", params=params)  # the phone has no login
    assert resp.status_code == 200, resp.text
    return resp.json()


def latest(client, me, alex):
    return client.get(f"/api/travellers/{alex}/where-are-you", headers=as_(me)).json()


def arrive(client, alex, trip):
    resp = client.post("/api/arrived", json={"tripId": trip, "travellerId": alex})
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------- a question ends with its trip
def test_a_question_belongs_to_the_trip_it_was_asked_on(client, setup):
    me, alex = setup

    question = ask(client, me, alex, "T-1").json()

    assert question["tripId"] == "T-1"
    assert phone_sees(client, alex, "T-1")["requestId"] == question["requestId"]
    assert phone_sees(client, alex, "T-2") is None  # the phone on another trip isn't asked


def test_arriving_ends_the_trips_question_answered_or_not(client, setup):
    me, alex = setup
    waiting = ask(client, me, alex, "T-1").json()
    reply(client, alex, waiting["requestId"])

    arrive(client, alex, "T-1")

    assert latest(client, me, alex) is None  # the dashboard's "Sally answered" note goes
    assert ask(client, me, alex, "T-2").json()["status"] == "asked"  # a fresh question on the next trip


def test_arriving_on_another_trip_leaves_the_question(client, setup):
    me, alex = setup
    question = ask(client, me, alex, "T-2").json()

    arrive(client, alex, "T-1")

    assert latest(client, me, alex)["requestId"] == question["requestId"]


def test_asking_on_a_new_trip_replaces_one_still_waiting_from_the_last(client, setup):
    me, alex = setup
    old = ask(client, me, alex, "T-1").json()

    new = ask(client, me, alex, "T-2").json()

    assert new["requestId"] != old["requestId"] and new["tripId"] == "T-2"
    assert ask(client, me, alex, "T-2").json()["requestId"] == new["requestId"]  # same trip: still one question


def reply(client, alex, request_id, answer="ok", lat=1.4, lng=103.9):
    return client.post("/api/where-are-you/reply", json={"travellerId": alex, "requestId": request_id,
                                                         "answer": answer, "lat": lat, "lng": lng})


def test_the_phone_sees_a_question_and_the_caregiver_sees_the_answer(client, setup, published):
    me, alex = setup
    asked = ask(client, me, alex)
    assert asked.status_code == 202
    assert asked.json()["status"] == "asked"
    assert asked.json()["askedBy"] == me

    question = phone_sees(client, alex)
    assert question["requestId"] == asked.json()["requestId"]

    answered = reply(client, alex, question["requestId"]).json()
    assert answered["status"] == "answered"
    assert answered["answer"] == "ok"
    assert (answered["lat"], answered["lng"]) == (1.4, 103.9)
    assert phone_sees(client, alex) is None  # answered, so the phone stops showing it
    latest = client.get(f"/api/travellers/{alex}/where-are-you", headers=as_(me)).json()
    assert latest["answer"] == "ok"
    assert [e["event"] for e in published(alex)] == ["where.asked", "where.answered"]


def test_asking_again_while_waiting_keeps_one_question(client, setup, published):
    me, alex = setup
    first = ask(client, me, alex).json()
    second = ask(client, me, alex).json()

    assert first["requestId"] == second["requestId"]
    assert len(published(alex)) == 1


def test_asking_after_an_answer_is_a_new_question(client, setup):
    me, alex = setup
    first = ask(client, me, alex).json()
    reply(client, alex, first["requestId"])

    second = ask(client, me, alex).json()

    assert second["requestId"] != first["requestId"]
    assert second["status"] == "asked"
    assert second["answer"] is None


def test_a_repeated_reply_counts_once(client, setup, published):
    me, alex = setup
    question = ask(client, me, alex).json()

    first = reply(client, alex, question["requestId"], "ok").json()
    second = reply(client, alex, question["requestId"], "help").json()  # a late retry of a different tap

    assert second["answer"] == "ok"
    assert second["answeredAt"] == first["answeredAt"]
    assert [e["event"] for e in published(alex)] == ["where.asked", "where.answered"]


def test_a_reply_to_an_old_question_is_not_found(client, setup):
    me, alex = setup
    ask(client, me, alex)

    assert reply(client, alex, "W-NOTTHIS").status_code == 404


def test_a_reply_with_no_gps_still_counts(client, setup):
    me, alex = setup
    question = ask(client, me, alex).json()

    answered = reply(client, alex, question["requestId"], lat=None, lng=None).json()

    assert answered["status"] == "answered"
    assert answered["lat"] is None


def test_nothing_asked_is_null(client, setup):
    me, alex = setup

    assert phone_sees(client, alex) is None
    assert client.get(f"/api/travellers/{alex}/where-are-you", headers=as_(me)).json() is None


def test_a_stranger_cannot_ask_or_see(client, setup):
    me, alex = setup
    stranger = new_caregiver()

    assert ask(client, stranger, alex).status_code == 404
    assert client.get(f"/api/travellers/{alex}/where-are-you", headers=as_(stranger)).status_code == 404


def test_a_linked_caregiver_sees_the_answer_to_someone_elses_question(client, setup):
    me, alex = setup
    partner = new_caregiver()
    client.post("/api/links", json={"travellerId": alex, "caregiverId": partner}, headers=as_(me))
    reply(client, alex, ask(client, me, alex).json()["requestId"])

    assert client.get(f"/api/travellers/{alex}/where-are-you", headers=as_(partner)).json()["answer"] == "ok"


def test_asking_needs_a_caregiver(client, setup):
    _, alex = setup

    assert client.post(f"/api/travellers/{alex}/where-are-you").status_code == 401
    assert client.post(f"/api/travellers/{alex}/where-are-you", headers=as_(alex)).status_code == 403


def test_a_bad_answer_is_refused(client, setup):
    me, alex = setup
    question = ask(client, me, alex).json()

    assert reply(client, alex, question["requestId"], answer="maybe").status_code == 422


def test_redis_down_tells_the_phone_to_try_again(client, setup, monkeypatch):
    me, alex = setup
    question = ask(client, me, alex).json()

    def down(*args, **kwargs):
        raise redis.ConnectionError("down")

    monkeypatch.setattr(service.where, "_reply", down)
    assert reply(client, alex, question["requestId"]).status_code == 503


class FakeLogin:
    def __init__(self, caller: Caller):
        self.caller = caller

    def authenticate(self, headers) -> Caller:
        return self.caller


def test_with_cognito_only_the_traveller_reads_or_answers_their_question(client, setup, monkeypatch):
    me, alex = setup
    question = ask(client, me, alex).json()
    monkeypatch.setattr(service, "settings", dataclasses.replace(service.settings, auth_mode="cognito"))
    monkeypatch.setattr(service, "authenticator", FakeLogin(Caller("U-SOMEONEELSE", "traveller")))

    assert client.get("/api/where-are-you", params={"travellerId": alex}).status_code == 403
    assert reply(client, alex, question["requestId"]).status_code == 403

    monkeypatch.setattr(service, "authenticator", FakeLogin(Caller(alex, "traveller")))
    assert reply(client, alex, question["requestId"]).status_code == 200
