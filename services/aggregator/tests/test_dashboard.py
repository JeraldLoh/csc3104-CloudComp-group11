"""Dashboard read API (/api/status/travellers, /api/decisions) and page, over HTTP.

Decisions are produced the real way (AggregatorService.handle), so the API is tested
against exactly what the service writes.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fakeredis
import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.service import RECENT_DECISIONS_KEY, AggregatorService

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "examples"
CHECKIN = json.loads((CONTRACTS / "checkin.json").read_text())
T0 = datetime(2026, 10, 12, 8, 0, 0, tzinfo=timezone.utc)


def vote(detector, verdict, confidence=0.8, reason="test", latency=12):
    return {"detectorId": detector, "verdict": verdict, "confidence": confidence,
            "problemType": "off_route" if verdict == "problem" else None, "reason": reason, "latencyMs": latency}


NORMAL = [vote("route-corridor", "normal", 0.9), vote("dwell-time", "normal", 0.8)]
PROBLEM = [vote("route-corridor", "problem", 0.9), vote("dwell-time", "problem", 0.8)]


def alert_ok(req: httpx.Request) -> httpx.Response:
    r = json.loads(req.content)
    return httpx.Response(200, json={"incidentId": f"{r['travellerId']}#{r['tripId']}#{r['problemType']}#{r['stepIndex']}",
                                     "created": True})


def scenario(decisions, requests, seed=None):
    """Run `decisions` [(traveller, trip, seq, votes, escalated)] through the service, then make
    `requests` [(method, path)] against the app. Returns the responses."""
    async def go():
        redis = fakeredis.FakeAsyncRedis(decode_responses=True)
        for key, values in (seed or {}).items():
            await redis.rpush(key, *values)
        async with httpx.AsyncClient(transport=httpx.MockTransport(alert_ok)) as alert_http:
            svc = AggregatorService(redis, alert_http, Settings(alert_url="http://alert-service:8000"))
            for i, (traveller, trip, seq, votes, escalated) in enumerate(decisions):
                checkin = {**CHECKIN, "travellerId": traveller, "tripId": trip, "seq": seq}
                await svc.handle(f"{trip}#{seq}", checkin, votes, escalated, now=T0 + timedelta(seconds=i))
            app = create_app(service=svc)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://aggregator") as c:
                return [await c.request(m, p) for m, p in requests]
    return asyncio.run(go())


# --- /api/decisions -----------------------------------------------------------------------------

def test_recent_decisions_newest_first():
    [r] = scenario([("U-1", "T-1", 1, NORMAL, False), ("U-2", "T-2", 1, PROBLEM, False)],
                   [("GET", "/api/decisions")])
    assert r.status_code == 200
    assert [d["requestId"] for d in r.json()] == ["T-2#1", "T-1#1"]


def test_decision_record_has_what_the_page_shows():
    [r] = scenario([("U-1", "T-1", 1, PROBLEM, True)], [("GET", "/api/decisions")])
    d = r.json()[0]
    for field in ("requestId", "travellerId", "tripId", "seq", "stepIndex", "decidedAt", "k", "escalated",
                  "votes", "decision", "problemType", "problemScore", "riskBefore", "riskAfter",
                  "incidentId", "alertCreated"):
        assert field in d, field
    assert d["k"] == 2 and d["escalated"] is True
    assert d["votes"][0] == {"detectorId": "route-corridor", "verdict": "problem", "confidence": 0.9,
                             "latencyMs": 12, "reason": "test"}
    assert d["alertCreated"] is True


def test_decisions_for_one_traveller():
    [r] = scenario([("U-1", "T-1", 1, NORMAL, False), ("U-2", "T-2", 1, NORMAL, False), ("U-1", "T-1", 2, NORMAL, False)],
                   [("GET", "/api/decisions?travellerId=U-1")])
    assert [d["requestId"] for d in r.json()] == ["T-1#2", "T-1#1"]


def test_limit():
    rows = [("U-1", "T-1", s, NORMAL, False) for s in range(1, 6)]
    [r] = scenario(rows, [("GET", "/api/decisions?limit=2")])
    assert [d["seq"] for d in r.json()] == [5, 4]


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "limit=abc", "travellerId="])
def test_bad_query_is_422_in_contract_format(query):
    [r] = scenario([], [("GET", f"/api/decisions?{query}")])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"
    assert r.json()["error"]["message"] == "Invalid query parameters"


def test_no_decisions_yet():
    [r] = scenario([], [("GET", "/api/decisions")])
    assert r.json() == []


def test_unreadable_records_are_skipped():
    good = json.dumps({"requestId": "T-9#1", "seq": 1})
    [r] = scenario([], [("GET", "/api/decisions")], seed={RECENT_DECISIONS_KEY: ["{not json", good, '"a string"']})
    assert [d["requestId"] for d in r.json()] == ["T-9#1"]


# --- /api/status/travellers -------------------------------------------------------------------------

def test_status_has_the_contract_fields():
    [r] = scenario([("U-1", "T-1", 1, NORMAL, False)], [("GET", "/api/status/travellers")])
    [row] = r.json()
    # contracts/api.md: [{"travellerId", "tripId", "riskLevel", "detectorsUsed"}]
    assert {k: row[k] for k in ("travellerId", "tripId", "riskLevel", "detectorsUsed")} == \
        {"travellerId": "U-1", "tripId": "T-1", "riskLevel": "normal", "detectorsUsed": 2}
    assert row["nextDetectors"] == 2
    assert row["lastDecision"] == "normal" and row["lastSeq"] == 1


def test_status_after_a_problem_is_elevated_with_4_next():
    [r] = scenario([("U-1", "T-1", 1, PROBLEM, False)], [("GET", "/api/status/travellers")])
    [row] = r.json()
    assert row["riskLevel"] == "elevated"
    assert row["riskScore"] == 0.5
    assert row["nextDetectors"] == 4
    assert row["calmStreak"] == 0


def test_elevated_travellers_are_listed_first():
    [r] = scenario([("U-calm", "T-1", 1, NORMAL, False), ("U-risky", "T-2", 1, PROBLEM, False),
                    ("U-later", "T-3", 1, NORMAL, False)],
                   [("GET", "/api/status/travellers")])
    assert [row["travellerId"] for row in r.json()] == ["U-risky", "U-later", "U-calm"]


def test_no_travellers_yet():
    [r] = scenario([], [("GET", "/api/status/travellers")])
    assert r.json() == []


def test_status_follows_risk_back_to_normal():
    rows = [("U-1", "T-1", 1, PROBLEM, False)] + [("U-1", "T-1", s, NORMAL, False) for s in (2, 3, 4)]
    [r] = scenario(rows, [("GET", "/api/status/travellers")])
    [row] = r.json()
    assert row["riskLevel"] == "normal" and row["nextDetectors"] == 2 and row["calmStreak"] == 3


# --- the page ---------------------------------------------------------------------------------------

def test_dashboard_page_is_served():
    [r] = scenario([], [("GET", "/dashboard")])
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>CareRoute Detection</title>" in r.text
    # It reads the two endpoints above, with relative URLs so it also works behind port-forward.
    assert '"api/status/travellers"' in r.text and '"api/decisions"' in r.text


def test_page_never_inserts_api_data_as_html():
    page = (Path(__file__).resolve().parents[1] / "app" / "static" / "dashboard.html").read_text()
    assert "innerHTML" not in page and "insertAdjacentHTML" not in page and "document.write" not in page
