"""AggregatorService against an in-memory Redis (fakeredis) and a fake Alert Service."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import fakeredis
import httpx
import pytest

from app.config import Settings
from app.service import (
    RECENT_DECISIONS_KEY,
    WEIGHTS_KEY,
    AggregatorService,
    answer_key,
    parse_weights,
    risk_key,
    traveller_decisions_key,
)

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "examples"
CHECKIN = json.loads((CONTRACTS / "checkin.json").read_text())          # T-001 seq 17, U-042
REPORT_FIELDS = set(json.loads((CONTRACTS / "problem-report.json").read_text()))
NOW = datetime(2026, 10, 12, 8, 15, 31, tzinfo=timezone.utc)


def v(detector, verdict, confidence=0.8, problem_type=None):
    return {"requestId": "T-001#17", "detectorId": detector, "verdict": verdict, "confidence": confidence,
            "problemType": problem_type or ("off_route" if verdict == "problem" else None),
            "reason": "test", "latencyMs": 12}


BOTH_PROBLEM = [v("route-corridor", "problem", 0.9), v("dwell-time", "problem", 0.7)]
BOTH_NORMAL = [v("route-corridor", "normal", 0.9), v("dwell-time", "normal", 0.7)]
SPLIT = [v("route-corridor", "problem", 0.8), v("dwell-time", "normal", 0.8)]


class AlertService:
    """Fake Alert Service: records reports; one incident per id like the real one."""

    def __init__(self, status=200, down=False):
        self.status = status
        self.down = down
        self.reports = []
        self.ids = set()

    async def handler(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("down")
        report = json.loads(req.content)
        self.reports.append(report)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"code": "unavailable"}})
        iid = f"{report['travellerId']}#{report['tripId']}#{report['problemType']}#{report['stepIndex']}"
        created = iid not in self.ids
        self.ids.add(iid)
        return httpx.Response(200, json={"incidentId": iid, "created": created})


def settings(**overrides) -> Settings:
    return Settings(**{"alert_url": "http://alert-service:8000", "key_ttl_s": 86400,
                       "decisions_per_traveller": 50, "decisions_recent": 200, **overrides})


def run(calls, alert=None, seed=None, **overrides):
    """calls: list of (request_id, votes[, escalated]). Returns (replies, redis state, alert)."""
    alert = alert or AlertService()

    async def go():
        redis = fakeredis.FakeAsyncRedis(decode_responses=True)
        for key, mapping in (seed or {}).items():
            await redis.hset(key, mapping=mapping)
        async with httpx.AsyncClient(transport=httpx.MockTransport(alert.handler)) as http:
            svc = AggregatorService(redis, http, settings(**overrides))
            replies = []
            for call in calls:
                request_id, votes, *rest = call
                checkin = {**CHECKIN, "seq": int(request_id.split("#")[1])}
                replies.append(await svc.handle(request_id, checkin, votes, escalated=bool(rest and rest[0]), now=NOW))
        state = {
            "risk": await redis.hgetall(risk_key("U-042")),
            "risk_ttl": await redis.ttl(risk_key("U-042")),
            "decisions": [json.loads(x) for x in await redis.lrange(traveller_decisions_key("U-042"), 0, -1)],
            "recent": [json.loads(x) for x in await redis.lrange(RECENT_DECISIONS_KEY, 0, -1)],
            "answers": {k: json.loads(await redis.get(k)) for k in await redis.keys("aggregated:*")},
        }
        return replies, state

    replies, state = asyncio.run(go())
    return replies, state, alert


# --- normal and problem decisions ---------------------------------------------------------------

def test_normal_decision_sends_no_alert_and_records_it():
    [reply], state, alert = run([("T-001#17", BOTH_NORMAL)])
    assert reply == {"decision": "normal", "problemType": None, "confidence": 1.0,
                     "needsMoreVotes": False, "reason": None, "riskLevel": "normal"}
    assert alert.reports == []
    assert state["risk"]["level"] == "normal"
    assert state["risk"]["detectorsUsed"] == "2"
    assert state["risk"]["updatedAt"] == "2026-10-12T08:15:31Z"
    assert len(state["decisions"]) == 1 and len(state["recent"]) == 1


def test_problem_is_reported_to_the_alert_service_and_raises_risk():
    [reply], state, alert = run([("T-001#17", BOTH_PROBLEM)])
    assert reply["decision"] == "problem" and reply["problemType"] == "off_route"
    assert reply["riskLevel"] == "elevated"
    [report] = alert.reports
    assert set(report) == REPORT_FIELDS
    assert report["reportId"] == "T-001#17" and report["detectorsUsed"] == 2
    assert state["risk"]["level"] == "elevated"
    record = state["decisions"][0]
    assert record["incidentId"] == "U-042#T-001#off_route#2"
    assert record["alertCreated"] is True


def test_decision_record_has_what_the_dashboard_needs():
    _, state, _ = run([("T-001#17", BOTH_PROBLEM, True)])
    record = state["decisions"][0]
    assert record["requestId"] == "T-001#17" and record["seq"] == 17
    assert record["k"] == 2 and record["escalated"] is True
    assert record["votes"][0] == {"detectorId": "route-corridor", "verdict": "problem", "confidence": 0.9,
                                  "latencyMs": 12, "reason": "test"}
    assert record["riskBefore"] == {"level": "normal", "score": 0.0}
    assert record["riskAfter"] == {"level": "elevated", "score": 0.5}
    assert record["decidedAt"] == "2026-10-12T08:15:31Z"
    assert state["recent"][0] == record


def test_keys_expire_after_a_day():
    _, state, _ = run([("T-001#17", BOTH_NORMAL)])
    assert 0 < state["risk_ttl"] <= 86400


# --- close calls change nothing -------------------------------------------------------------------

def test_close_call_asks_for_more_votes_without_side_effects():
    [reply], state, alert = run([("T-001#17", SPLIT)])
    assert reply["decision"] == "inconclusive"
    assert reply["needsMoreVotes"] is True and reply["reason"] == "split"
    assert alert.reports == []
    assert state["risk"] == {} and state["decisions"] == [] and state["answers"] == {}


def test_close_call_then_four_votes_decides():
    four = SPLIT + [v("heading-speed", "problem", 0.7), v("ml-anomaly", "problem", 0.6)]
    replies, state, alert = run([("T-001#17", SPLIT), ("T-001#17", four, True)])
    assert [r["decision"] for r in replies] == ["inconclusive", "problem"]
    assert len(alert.reports) == 1 and alert.reports[0]["detectorsUsed"] == 4
    assert len(state["decisions"]) == 1 and state["decisions"][0]["k"] == 4


def test_final_inconclusive_records_and_raises_risk_without_alert():
    blind = [v("route-corridor", "problem", 0.9)] + [v(d, "abstain", 0.0) for d in ("dwell-time", "heading-speed", "ml-anomaly")]
    [reply], state, alert = run([("T-001#17", blind)])
    assert reply["decision"] == "inconclusive" and reply["needsMoreVotes"] is False
    assert alert.reports == []
    assert state["risk"]["level"] == "elevated"
    assert state["decisions"][0]["decision"] == "inconclusive"


# --- repeats are safe ---------------------------------------------------------------------------

def test_repeat_request_returns_the_saved_answer_without_side_effects():
    replies, state, alert = run([("T-001#17", BOTH_PROBLEM), ("T-001#17", BOTH_PROBLEM)])
    assert replies[0] == replies[1]
    assert len(alert.reports) == 1
    assert len(state["decisions"]) == 1
    assert state["risk"]["score"] == "0.5000"   # not raised twice


def test_different_checkins_are_both_processed():
    replies, state, _ = run([("T-001#17", BOTH_PROBLEM), ("T-001#18", BOTH_NORMAL)])
    assert [r["decision"] for r in replies] == ["problem", "normal"]
    assert len(state["decisions"]) == 2
    assert state["decisions"][0]["seq"] == 18   # newest first


# --- Alert Service failures: raise, change nothing ------------------------------------------------------

def test_alert_service_error_raises():
    with pytest.raises(httpx.HTTPStatusError):
        run([("T-001#17", BOTH_PROBLEM)], alert=AlertService(status=503))


def test_alert_service_down_raises():
    with pytest.raises(httpx.ConnectError):
        run([("T-001#17", BOTH_PROBLEM)], alert=AlertService(down=True))


def test_alert_failure_saves_nothing_and_the_retry_succeeds_cleanly():
    alert = AlertService()
    attempts = 0

    async def flaky(req):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={})
        return await alert.handler(req)

    async def go():
        redis = fakeredis.FakeAsyncRedis(decode_responses=True)
        async with httpx.AsyncClient(transport=httpx.MockTransport(flaky)) as http:
            svc = AggregatorService(redis, http, settings())
            with pytest.raises(httpx.HTTPStatusError):
                await svc.handle("T-001#17", CHECKIN, BOTH_PROBLEM, now=NOW)
            assert await redis.exists(risk_key("U-042")) == 0          # nothing half-written
            reply = await svc.handle("T-001#17", CHECKIN, BOTH_PROBLEM, now=NOW)
            return reply, await redis.llen(traveller_decisions_key("U-042"))

    reply, n = asyncio.run(go())
    assert reply["decision"] == "problem" and n == 1


# --- reliability weights ---------------------------------------------------------------------------

def test_detector_weights_from_redis_are_used():
    votes = [v("route-corridor", "problem", 0.5), v("dwell-time", "normal", 0.9)]
    [plain], _, _ = run([("T-001#17", votes)])
    [weighted], _, _ = run([("T-001#17", votes)], seed={WEIGHTS_KEY: {"dwell-time": "0.3"}})
    assert plain["decision"] == "normal"
    assert weighted["decision"] == "problem"


def test_bad_weights_are_ignored():
    assert parse_weights({"route-corridor": "0.9", "dwell-time": "lots"}) == {"route-corridor": 0.9}
    assert parse_weights(None) == {}


# --- risk over a journey, and record limits ------------------------------------------------------------

def test_risk_over_a_journey():
    calls = [("T-001#17", BOTH_PROBLEM)] + [(f"T-001#{s}", BOTH_NORMAL) for s in (18, 19, 20)]
    replies, state, _ = run(calls)
    assert [r["riskLevel"] for r in replies] == ["elevated", "elevated", "elevated", "normal"]
    assert state["risk"]["calmStreak"] == "3"


def test_decision_lists_are_trimmed():
    calls = [(f"T-001#{s}", BOTH_NORMAL) for s in range(1, 6)]
    _, state, _ = run(calls, decisions_per_traveller=3, decisions_recent=4)
    assert [d["seq"] for d in state["decisions"]] == [5, 4, 3]
    assert len(state["recent"]) == 4
