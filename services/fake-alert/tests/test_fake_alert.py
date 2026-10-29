import json
import logging
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app, incident_id

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "examples"
REPORT = json.loads((CONTRACTS / "problem-report.json").read_text())
INCIDENT_FIELDS = set(json.loads((CONTRACTS / "incident.json").read_text()))


@pytest.fixture
def c() -> TestClient:
    return TestClient(create_app(mode="ok", delay_ms=0))


def report(c: TestClient, body=REPORT):
    return c.post("/internal/problems", json=body)


# --- one incident per problem ------------------------------------------------------------

def test_first_report_creates_an_incident(c):
    r = report(c)
    assert r.status_code == 200
    assert r.json() == {"incidentId": "U-042#T-001#off_route#2", "created": True}


def test_repeat_report_is_not_a_new_incident(c):
    report(c)
    r = report(c)
    assert r.json() == {"incidentId": "U-042#T-001#off_route#2", "created": False}
    [incident] = c.get("/api/incidents").json()
    assert incident["detectionCount"] == 2


def test_different_step_is_a_different_incident(c):
    report(c)
    r = report(c, {**REPORT, "stepIndex": 3})
    assert r.json()["created"] is True
    assert len(c.get("/api/incidents").json()) == 2


def test_different_problem_type_is_a_different_incident(c):
    report(c)
    assert report(c, {**REPORT, "problemType": "wrong_direction"}).json()["created"] is True


def test_unknown_step_gives_an_empty_last_part():
    assert incident_id({**REPORT, "stepIndex": None}) == "U-042#T-001#off_route#"


def test_new_incident_is_logged_as_alert(c, caplog):
    with caplog.at_level(logging.INFO, logger="fake-alert"):
        report(c)
        report(c)
    alerts = [r for r in caplog.records if r.getMessage().startswith("ALERT")]
    assert len(alerts) == 1


# --- incidents list (same shape as incident.json) ----------------------------------------

def test_incident_has_the_contract_fields(c):
    report(c)
    [incident] = c.get("/api/incidents").json()
    assert set(incident) == INCIDENT_FIELDS
    assert incident["state"] == "open"
    assert incident["lastLocation"] == REPORT["location"]
    assert incident["confidence"] == REPORT["confidence"]


def test_repeat_updates_latest_details(c):
    report(c)
    report(c, {**REPORT, "confidence": 0.95, "location": {"lat": 1.41, "lng": 103.91}})
    [incident] = c.get("/api/incidents").json()
    assert incident["confidence"] == 0.95
    assert incident["lastLocation"] == {"lat": 1.41, "lng": 103.91}


def test_filter_by_traveller(c):
    report(c)
    report(c, {**REPORT, "travellerId": "U-099"})
    assert [i["travellerId"] for i in c.get("/api/incidents?travellerId=U-099").json()] == ["U-099"]
    assert len(c.get("/api/incidents").json()) == 2


# --- bad input -------------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["travellerId", "tripId", "problemType"])
def test_missing_required_field_is_rejected(c, field):
    body = {k: v for k, v in REPORT.items() if k != field}
    r = report(c, body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"
    assert c.get("/api/incidents").json() == []


# --- fault injection ---------------------------------------------------------------------------

def test_error_mode_returns_503_and_stores_nothing(c):
    c.post("/control", json={"mode": "error"})
    r = report(c)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "unavailable"
    assert c.get("/api/incidents").json() == []


def test_recovers_after_error_mode(c):
    c.post("/control", json={"mode": "error"})
    report(c)
    c.post("/control", json={"mode": "ok"})
    assert report(c).json()["created"] is True


def test_delay(c):
    c.post("/control", json={"delayMs": 100})
    start = time.perf_counter()
    report(c)
    assert time.perf_counter() - start >= 0.1


@pytest.mark.parametrize("body", [{"mode": "broken"}, {"delayMs": -5}])
def test_control_rejects_bad_values(c, body):
    assert c.post("/control", json=body).status_code == 422
    assert c.get("/control").json()["mode"] == "ok"


def test_reset_clears_incidents(c):
    report(c)
    assert c.post("/control/reset").json()["incidents"] == 0
    assert report(c).json()["created"] is True


def test_mode_from_env(monkeypatch):
    monkeypatch.setenv("MODE", "error")
    monkeypatch.setenv("DELAY_MS", "7")
    assert TestClient(create_app()).get("/control").json() == {"mode": "error", "delayMs": 7, "incidents": 0}


def test_health(c):
    assert c.get("/health").json() == {"status": "ok"}
