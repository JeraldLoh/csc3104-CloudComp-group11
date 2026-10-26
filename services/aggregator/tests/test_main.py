"""POST /internal/votes over HTTP (httpx.ASGITransport), with fakeredis and a fake Alert Service."""
import asyncio
import json
from pathlib import Path

import fakeredis
import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.service import AggregatorService

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "examples"
CHECKIN = json.loads((CONTRACTS / "checkin.json").read_text())
REPLY_FIELDS = {"decision", "problemType", "confidence", "needsMoreVotes", "reason", "riskLevel"}


def vote(detector, verdict, confidence=0.8):
    return {"requestId": "T-001#17", "detectorId": detector, "verdict": verdict, "confidence": confidence,
            "problemType": "off_route" if verdict == "problem" else None, "reason": "test", "latencyMs": 5}


BODY = {"requestId": "T-001#17", "checkin": CHECKIN, "riskLevel": "normal", "escalated": False,
        "votes": [vote("route-corridor", "problem", 0.9), vote("dwell-time", "problem", 0.7)]}


def alert_ok(req: httpx.Request) -> httpx.Response:
    r = json.loads(req.content)
    return httpx.Response(200, json={"incidentId": f"{r['travellerId']}#{r['tripId']}#{r['problemType']}#{r['stepIndex']}",
                                     "created": True})


def call(method, path, body=None, alert=alert_ok, redis=None):
    """One request against the app; returns the httpx.Response."""
    async def go():
        r = redis or fakeredis.FakeAsyncRedis(decode_responses=True)
        async with httpx.AsyncClient(transport=httpx.MockTransport(alert)) as alert_http:
            svc = AggregatorService(r, alert_http, Settings(alert_url="http://alert-service:8000"))
            app = create_app(service=svc)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                         base_url="http://aggregator") as client:
                return await client.request(method, path, json=body)
    return asyncio.run(go())


# --- happy path -------------------------------------------------------------------------------

def test_votes_returns_the_decision():
    r = call("POST", "/internal/votes", BODY)
    assert r.status_code == 200
    assert set(r.json()) == REPLY_FIELDS
    assert r.json()["decision"] == "problem"
    assert r.json()["riskLevel"] == "elevated"


def test_contract_minimum_body_is_enough():
    # contracts/api.md: {"requestId", "checkin", "votes"}; riskLevel and escalated are optional.
    body = {k: BODY[k] for k in ("requestId", "checkin", "votes")}
    assert call("POST", "/internal/votes", body).status_code == 200


def test_close_call_reply_asks_for_more_votes():
    body = {**BODY, "votes": [vote("route-corridor", "problem"), vote("dwell-time", "normal")]}
    r = call("POST", "/internal/votes", body)
    assert r.status_code == 200
    assert r.json()["needsMoreVotes"] is True
    assert r.json()["reason"] == "single_voter"   # dwell-time can't judge off_route


def test_extra_fields_are_ignored():
    assert call("POST", "/internal/votes", {**BODY, "somethingNew": 1}).status_code == 200


# --- bad requests ----------------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {k: v for k, v in BODY.items() if k != "requestId"},
    {k: v for k, v in BODY.items() if k != "votes"},
    {**BODY, "checkin": {k: v for k, v in CHECKIN.items() if k != "travellerId"}},
    {**BODY, "checkin": {k: v for k, v in CHECKIN.items() if k != "tripId"}},
    {**BODY, "requestId": ""},
    {**BODY, "votes": "lots"},
])
def test_invalid_body_is_422_in_contract_format(body):
    r = call("POST", "/internal/votes", body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"
    assert r.json()["error"]["details"]


# --- failures: 503 so the Coordinator retries ---------------------------------------------------------

def test_alert_service_error_is_503():
    r = call("POST", "/internal/votes", BODY, alert=lambda req: httpx.Response(503, json={}))
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "alert_unavailable"


def test_alert_service_down_is_503():
    def down(req):
        raise httpx.ConnectError("down")
    r = call("POST", "/internal/votes", BODY, alert=down)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "alert_unavailable"


class BrokenRedis:
    async def get(self, *_):
        raise ConnectionError("redis down")

    async def ping(self):
        raise ConnectionError("redis down")


def test_redis_down_is_503():
    r = call("POST", "/internal/votes", BODY, redis=BrokenRedis())
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "unavailable"


# --- probes ------------------------------------------------------------------------------------------

def test_health():
    r = call("GET", "/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_ready_when_redis_is_up():
    assert call("GET", "/ready").json() == {"status": "ready"}


def test_not_ready_when_redis_is_down():
    r = call("GET", "/ready", redis=BrokenRedis())
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "not_ready"


def test_situation_is_accepted_and_optional():
    assert call("POST", "/internal/votes", {**BODY, "situation": "riding"}).status_code == 200
    assert call("POST", "/internal/votes", {k: v for k, v in BODY.items() if k != "situation"}).status_code == 200
