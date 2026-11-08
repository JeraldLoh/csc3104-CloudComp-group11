import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import Behaviour, create_app, from_env, inside, parse_box

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "examples"
REQUEST = json.loads((CONTRACTS / "detector-request.json").read_text())   # lat 1.4052, lng 103.9024
VOTE_FIELDS = set(json.loads((CONTRACTS / "detector-vote.json").read_text()))
OFF_ROUTE = {**REQUEST, "checkin": {**REQUEST["checkin"], "lat": 1.45, "lng": 103.95}}


def client(**kwargs) -> TestClient:
    return TestClient(create_app(Behaviour(**kwargs)))


def detect(c: TestClient, request=REQUEST) -> dict:
    r = c.post("/internal/detect", json=request)
    assert r.status_code == 200, r.text
    return r.json()


# --- matches the contract ---------------------------------------------------------------

def test_vote_has_the_contract_fields():
    assert set(detect(client())) == VOTE_FIELDS


def test_vote_echoes_request_id_and_uses_its_detector_id():
    vote = detect(client(detector_id="dwell-time"))
    assert vote["requestId"] == "T-001#17"
    assert vote["detectorId"] == "dwell-time"


def test_health():
    assert client().get("/health").json() == {"status": "ok"}


# --- modes ---------------------------------------------------------------------------------

def test_auto_on_route_is_normal():
    vote = detect(client(mode="auto"))
    assert vote["verdict"] == "normal"
    assert vote["problemType"] is None


def test_auto_off_route_is_problem():
    vote = detect(client(mode="auto", problem_type="off_route", confidence=0.9), OFF_ROUTE)
    assert vote["verdict"] == "problem"
    assert vote["problemType"] == "off_route"
    assert vote["confidence"] == 0.9


def test_wrong_mode_says_the_opposite_of_auto():
    c = client(mode="wrong")
    assert detect(c)["verdict"] == "problem"
    assert detect(c, OFF_ROUTE)["verdict"] == "normal"


@pytest.mark.parametrize("mode", ["normal", "problem"])
def test_fixed_modes_ignore_position(mode):
    c = client(mode=mode)
    assert detect(c)["verdict"] == mode
    assert detect(c, OFF_ROUTE)["verdict"] == mode


def test_abstain_mode_has_zero_confidence():
    vote = detect(client(mode="abstain", confidence=0.9))
    assert vote["verdict"] == "abstain"
    assert vote["confidence"] == 0.0
    assert vote["problemType"] is None


def test_error_mode_returns_500():
    r = client(mode="error").post("/internal/detect", json=REQUEST)
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "fake_error"


def test_missing_position_is_not_a_problem():
    request = {**REQUEST, "checkin": {"tripId": "T-001", "seq": 1}}
    assert detect(client(mode="auto"), request)["verdict"] == "normal"


def test_missing_checkin_does_not_crash():
    assert detect(client(mode="auto"), {"requestId": "T-001#1"})["verdict"] == "normal"


# --- delay ------------------------------------------------------------------------------------

def test_delay_makes_it_slow_and_is_reported():
    c = client(delay_ms=120)
    start = time.perf_counter()
    vote = detect(c)
    assert time.perf_counter() - start >= 0.12
    assert vote["latencyMs"] >= 120


# --- live control ---------------------------------------------------------------------------------

def test_control_changes_behaviour_while_running():
    c = client(mode="normal")
    assert detect(c)["verdict"] == "normal"
    r = c.post("/control", json={"mode": "problem", "delayMs": 5})
    assert r.status_code == 200
    assert r.json()["mode"] == "problem"
    assert r.json()["delay_ms"] == 5
    assert detect(c)["verdict"] == "problem"


def test_control_only_changes_given_fields():
    c = client(mode="auto", confidence=0.7, detector_id="ml-anomaly")
    after = c.post("/control", json={"delayMs": 10}).json()
    assert after["mode"] == "auto"
    assert after["confidence"] == 0.7
    assert after["detector_id"] == "ml-anomaly"


def test_get_control_shows_current_behaviour():
    assert client(mode="wrong").get("/control").json()["mode"] == "wrong"


@pytest.mark.parametrize("body", [{"mode": "sleepy"}, {"confidence": 1.5}, {"delayMs": -1}])
def test_control_rejects_bad_values(body):
    c = client()
    assert c.post("/control", json=body).status_code == 422
    assert c.get("/control").json()["mode"] == "auto"   # unchanged


# --- configuration -------------------------------------------------------------------------------

def test_inside_box_edges_count_as_inside():
    box = (1.0, 103.0, 2.0, 104.0)
    assert inside(box, 1.0, 103.0) and inside(box, 2.0, 104.0)
    assert not inside(box, 2.0001, 103.5)


def test_parse_box():
    assert parse_box("1.40,103.89,1.41,103.91") == (1.40, 103.89, 1.41, 103.91)


@pytest.mark.parametrize("spec", ["1,2,3", "1.5,103,1.4,104", "a,b,c,d"])
def test_parse_box_rejects_bad_input(spec):
    with pytest.raises(ValueError):
        parse_box(spec)


def test_from_env(monkeypatch):
    monkeypatch.setenv("DETECTOR_ID", "heading-speed")
    monkeypatch.setenv("MODE", "problem")
    monkeypatch.setenv("PROBLEM_TYPE", "wrong_direction")
    monkeypatch.setenv("CONFIDENCE", "0.6")
    monkeypatch.setenv("DELAY_MS", "50")
    monkeypatch.setenv("AUTO_BOX", "1,103,2,104")
    assert from_env() == Behaviour("heading-speed", "problem", "wrong_direction", 0.6, 50, (1.0, 103.0, 2.0, 104.0))
