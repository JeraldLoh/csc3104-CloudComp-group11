import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import Behaviour, create_app, from_env

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "examples"
REQUEST = json.loads((CONTRACTS / "detector-request.json").read_text())
VOTE_FIELDS = set(json.loads((CONTRACTS / "detector-vote.json").read_text()))
UPSTREAM = "http://detector-route:8000"


def real_detector(verdict="normal", problem_type=None, status=200):
    """A stand-in for the real detector. Records what it was asked."""
    asked = []

    def handler(req: httpx.Request) -> httpx.Response:
        asked.append(json.loads(req.content))
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json={"requestId": "T-001#17", "detectorId": "route-corridor", "verdict": verdict,
                                         "problemType": problem_type, "confidence": 0.9, "reason": "real",
                                         "latencyMs": 3})
    return httpx.MockTransport(handler), asked


def switch(mode="pass", transport=None, **kwargs) -> TestClient:
    transport = transport or real_detector()[0]
    return TestClient(create_app(Behaviour(upstream=UPSTREAM, mode=mode, **kwargs), upstream_transport=transport))


def detect(c: TestClient) -> dict:
    r = c.post("/internal/detect", json=REQUEST)
    assert r.status_code == 200, r.text
    return r.json()


# --- the modes -----------------------------------------------------------------------------------

def test_pass_returns_the_real_vote_unchanged():
    transport, asked = real_detector("problem", "off_route")
    vote = detect(switch("pass", transport))
    assert asked == [REQUEST]
    assert set(vote) == VOTE_FIELDS
    assert (vote["verdict"], vote["problemType"], vote["confidence"], vote["reason"]) == ("problem", "off_route", 0.9, "real")


@pytest.mark.parametrize("real, problem_type, expected", [
    ("problem", "off_route", ("normal", None)),
    ("normal", None, ("problem", "long_dwell")),
    ("abstain", None, ("abstain", None)),
])
def test_wrong_inverts_the_real_vote(real, problem_type, expected):
    transport, _ = real_detector(real, problem_type)
    vote = detect(switch("wrong", transport, problem_type="long_dwell"))
    assert (vote["verdict"], vote["problemType"]) == expected


def test_error_mode_returns_500_without_asking_the_real_detector():
    transport, asked = real_detector()
    r = switch("error", transport).post("/internal/detect", json=REQUEST)
    assert r.status_code == 500 and r.json()["error"]["code"] == "switch_error"
    assert asked == []


def test_real_detector_down_is_an_error_like_a_down_detector():
    transport, _ = real_detector(status=503)
    assert switch("pass", transport).post("/internal/detect", json=REQUEST).status_code == 502


def test_delay_makes_it_slow_and_is_reported():
    start = time.perf_counter()
    vote = detect(switch("pass", delay_ms=100))
    assert time.perf_counter() - start >= 0.1 and vote["latencyMs"] >= 100


def test_health():
    assert switch().get("/health").json() == {"status": "ok"}


# --- live control ------------------------------

def test_control_changes_behaviour_while_running():
    transport, _ = real_detector("normal")
    c = switch("pass", transport, problem_type="off_route")
    assert detect(c)["verdict"] == "normal"
    r = c.post("/control", json={"mode": "wrong", "delayMs": 5})
    assert r.status_code == 200 and r.json()["mode"] == "wrong" and r.json()["delay_ms"] == 5
    assert detect(c)["verdict"] == "problem"


def test_control_only_changes_given_fields():
    after = switch("wrong", problem_type="long_dwell").post("/control", json={"delayMs": 10}).json()
    assert (after["mode"], after["problem_type"], after["upstream"]) == ("wrong", "long_dwell", UPSTREAM)


def test_get_control_shows_current_behaviour():
    assert switch("error").get("/control").json()["mode"] == "error"


@pytest.mark.parametrize("body", [{"mode": "sleepy"}, {"mode": "auto"}, {"delayMs": -1}])
def test_control_rejects_bad_values(body):
    c = switch()
    assert c.post("/control", json=body).status_code == 422
    assert c.get("/control").json()["mode"] == "pass"   # unchanged


# --- configuration -------------------------------------------------------------------------------

def test_from_env(monkeypatch):
    monkeypatch.setenv("UPSTREAM_URL", UPSTREAM)
    monkeypatch.setenv("MODE", "wrong")
    monkeypatch.setenv("PROBLEM_TYPE", "wrong_direction")
    monkeypatch.setenv("DELAY_MS", "50")
    assert from_env() == Behaviour(UPSTREAM, "wrong", "wrong_direction", 50)


def test_upstream_is_required(monkeypatch):
    monkeypatch.delenv("UPSTREAM_URL", raising=False)
    with pytest.raises(RuntimeError, match="UPSTREAM_URL"):
        from_env()
