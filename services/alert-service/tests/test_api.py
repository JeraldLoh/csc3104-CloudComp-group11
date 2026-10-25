"""HTTP endpoints from contracts/api.md (Alert Service section)."""
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(dynamodb):  # dynamodb: make sure LocalStack is up and the tables exist
    return TestClient(app)


def path_id(iid: str) -> str:
    """Incident IDs contain '#', which must be URL-encoded in a path."""
    return quote(iid, safe="")


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_post_problem_twice_gives_one_incident(client, report):
    first = client.post("/internal/problems", json=report)
    second = client.post("/internal/problems", json=report)

    assert first.status_code == 200
    assert first.json() == {"incidentId": f"{report['travellerId']}#T-001#off_route#2", "created": True}
    assert second.json() == {"incidentId": first.json()["incidentId"], "created": False}

    listed = client.get("/api/incidents", params={"travellerId": report["travellerId"]}).json()
    assert len(listed) == 1
    assert listed[0]["detectionCount"] == 2


def test_heartbeat_report_is_accepted(client, no_response_report):
    resp = client.post("/internal/problems", json=no_response_report)

    assert resp.status_code == 200
    assert resp.json()["incidentId"].endswith("#T-001#no_response#2")


def test_report_with_gaps_is_still_accepted(client, report):
    """Unknown type, unknown step, no GPS: all allowed, and the caregiver is still alerted."""
    gaps = {**report, "problemType": "unknown", "stepIndex": None, "location": {"lat": None, "lng": None}}

    resp = client.post("/internal/problems", json=gaps)

    assert resp.status_code == 200
    assert resp.json() == {"incidentId": f"{report['travellerId']}#T-001#unknown#unknown", "created": True}


def test_ack_and_resolve(client, report):
    iid = client.post("/internal/problems", json=report).json()["incidentId"]
    headers = {"X-User-Id": "G-007"}

    acked = client.post(f"/api/incidents/{path_id(iid)}/ack", headers=headers)
    assert acked.status_code == 200
    assert acked.json()["state"] == "acknowledged"
    assert acked.json()["acknowledgedBy"] == "G-007"

    resolved = client.post(f"/api/incidents/{path_id(iid)}/resolve", headers=headers)
    assert resolved.json()["state"] == "resolved"

    conflict = client.post(f"/api/incidents/{path_id(iid)}/ack", headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "conflict"


def test_unknown_incident_is_404_in_contract_format(client):
    resp = client.post(f"/api/incidents/{path_id('U-nobody#T-000#off_route#0')}/resolve")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_invalid_problem_is_rejected_in_contract_format(client, report):
    bad = {**report, "problemType": "lost_umbrella"}
    resp = client.post("/internal/problems", json=bad)

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid"
    assert "problemType" in resp.json()["error"]["message"]
