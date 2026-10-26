import json
import logging
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest
from fastapi.testclient import TestClient

from detectors import app as app_module
from detectors.app import REGISTRY, create_app
from detectors.config import Settings

T0 = datetime(2026, 10, 12, 8, 0, 0, tzinfo=timezone.utc)
M = 1 / 111_195  # one metre of latitude, in degrees
PATH = [[1.40, 103.9], [1.41, 103.9]]
STEPS = [{"stepIndex": 0, "mode": "walk"}]


def checkin(seq, lat, every=10):
    return {
        "tripId": "T-001",
        "travellerId": "U-042",
        "seq": seq,
        "timestamp": (T0 + timedelta(seconds=every * seq)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lat": lat,
        "lng": 103.9,
        "accuracyM": 10.0,
        "speedMps": 1.3,
        "headingDeg": 0.0,
        "stepIndex": 0,
        "routeVersion": 3,
        "status": "active",
    }


def seed(r, recent):
    r.set("trip:T-001:route", json.dumps({"steps": STEPS, "path": PATH}))
    r.hset("trip:T-001:context", mapping={"stepIndex": 0, "routeVersion": 3, "status": "active"})
    for c in recent:  # newest first
        r.lpush("trip:T-001:recent", json.dumps(c))


def body(current):
    return {"requestId": "T-001#9", "riskLevel": "normal", "checkin": current}


def client_for(detector_id, server=None):
    server = server or fakeredis.FakeServer()
    r = fakeredis.FakeRedis(server=server, decode_responses=True)
    return TestClient(create_app(Settings(detector_id=detector_id), r)), r, server


@pytest.mark.parametrize(
    "detector_id,recent,verdict,problem",
    [
        # 300 m east of the path
        ("route-corridor", [{**checkin(i, 1.405), "lng": 103.9027} for i in range(6)], "problem", "off_route"),
        # walking south against a northbound path
        ("heading-speed", [{**checkin(i, 1.405 - i * 13 * M), "headingDeg": 180.0} for i in range(6)], "problem", "wrong_direction"),
        # standing still for 360 s
        ("dwell-time", [checkin(i, 1.405, every=40) for i in range(10)], "problem", "long_dwell"),
        # standing 300 m east of the path, not getting closer to the end
        ("route-progress", [{**checkin(i, 1.405), "lng": 103.9027} for i in range(6)], "problem", "off_route"),
    ],
)
def test_detect_happy_path(detector_id, recent, verdict, problem):
    client, r, _ = client_for(detector_id)
    seed(r, recent[:-1][::-1])
    with client:
        resp = client.post("/internal/detect", json=body(recent[-1]))
    assert resp.status_code == 200
    data = resp.json()
    assert data["detectorId"] == detector_id and data["requestId"] == "T-001#9"
    assert data["verdict"] == verdict and data["problemType"] == problem
    assert data["latencyMs"] > 0
    assert set(data) == {"requestId", "detectorId", "verdict", "problemType", "confidence", "reason", "latencyMs"}


def test_no_route_in_redis_abstains():
    client, _, _ = client_for("route-corridor")
    with client:
        data = client.post("/internal/detect", json=body(checkin(1, 1.405))).json()
    assert data["verdict"] == "abstain" and data["confidence"] == 0 and data["problemType"] is None


def test_redis_down_abstains_on_detect_and_503_on_ready():
    client, r, server = client_for("route-corridor")
    seed(r, [])
    with client:
        assert client.get("/ready").status_code == 200
        server.connected = False
        resp = client.post("/internal/detect", json=body(checkin(1, 1.405)))
        assert resp.status_code == 200
        data = resp.json()
        assert data["verdict"] == "abstain" and data["reason"] == "internal error: ConnectionError"
        assert data["detectorId"] == "route-corridor"
        assert client.get("/ready").status_code == 503
        assert client.get("/health").json() == {"status": "ok"}  # liveness ignores Redis


def test_detector_crash_abstains(monkeypatch):
    def boom(*_):
        raise ValueError("bug")

    monkeypatch.setitem(REGISTRY, "route-corridor", boom)
    client, r, _ = client_for("route-corridor")
    seed(r, [])
    with client:
        data = client.post("/internal/detect", json=body(checkin(1, 1.405))).json()
    assert data["verdict"] == "abstain" and data["reason"] == "internal error: ValueError"


def test_bad_body_is_422():
    client, _, _ = client_for("route-corridor")
    with client:
        assert client.post("/internal/detect", json={"requestId": "x"}).status_code == 422
        assert client.post("/internal/detect", content="nope").status_code == 422


def test_unknown_detector_id_fails_fast():
    for detector_id in (None, "ml-anomaly"):
        with pytest.raises(RuntimeError, match="DETECTOR_ID"):
            with TestClient(create_app(Settings(detector_id=detector_id), fakeredis.FakeRedis())):
                pass


def test_logs_one_json_line_per_request(caplog):
    client, r, _ = client_for("heading-speed")
    seed(r, [])
    with caplog.at_level(logging.INFO, logger="detectors"), client:
        client.post("/internal/detect", json=body(checkin(1, 1.405)))
    line = json.loads(next(rec.getMessage() for rec in caplog.records if rec.name == "detectors"))
    assert line["requestId"] == "T-001#9" and line["detectorId"] == "heading-speed"
    assert line["verdict"] == "abstain" and "latencyMs" in line


def test_module_app_exists():
    assert app_module.app.title


def test_web_checkin_with_nulls_is_not_rejected():
    client, r, _ = client_for("route-corridor")
    seed(r, [])
    nulls = {**checkin(1, 1.405), "stepIndex": None, "routeVersion": None, "speedMps": None, "headingDeg": None, "batteryPct": None}
    with client:
        resp = client.post("/internal/detect", json=body(nulls))
    assert resp.status_code == 200 and resp.json()["verdict"] in {"normal", "problem", "abstain"}
