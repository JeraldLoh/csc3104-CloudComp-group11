"""Pipeline against an in-memory Redis (fakeredis), with fake detectors and a fake Alert Service
behind httpx.MockTransport. Decisions are real (decision.py, voting.py)."""
import asyncio
import json
from pathlib import Path

import fakeredis
import httpx
import pytest

from app.config import Settings
from app.decision import RECENT_DECISIONS_KEY, WEIGHTS_KEY, DecisionRecorder, decided_key, risk_key, \
    traveller_decisions_key
from app.detectors import DetectorClient
from app.pipeline import Pipeline
from app.selection import parse_urls

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "examples"
CHECKIN = json.loads((CONTRACTS / "checkin.json").read_text())        # T-001, seq 17, U-042, step 2
DETECTOR_REQUEST_FIELDS = set(json.loads((CONTRACTS / "detector-request.json").read_text()))
REPORT_FIELDS = set(json.loads((CONTRACTS / "problem-report.json").read_text()))
HOSTS = {"detector-route": "route-corridor", "detector-heading": "heading-speed",
         "detector-dwell": "dwell-time", "detector-progress": "route-progress"}
OWN_TYPE = {"route-corridor": "off_route", "heading-speed": "wrong_direction",
            "dwell-time": "long_dwell", "route-progress": "off_route"}


class World:
    """Fake detectors and Alert Service. A detector answers `detector_votes[name]`: a verdict,
    "timeout", "down", or ("abstain", reason). The Alert Service keeps one incident per id."""

    def __init__(self, detector_votes=None, alert_status=200, alert_down=False, alert_fails_for=None):
        self.detector_votes = {"route-corridor": "normal", "heading-speed": "normal",
                               "dwell-time": "normal", "route-progress": "normal", **(detector_votes or {})}
        self.alert_status = alert_status
        self.alert_down = alert_down
        self.alert_fails_for = alert_fails_for   # a problemType the Alert Service answers 503 to
        self.detector_calls = []   # (detectorId, request body)
        self.reports = []
        self.incidents = set()

    async def handler(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if req.url.host == "alert-service":
            return self._alert(body)
        name = HOSTS[req.url.host]
        self.detector_calls.append((name, body))
        behaviour = self.detector_votes[name]
        if behaviour == "timeout":
            await asyncio.sleep(1)
        if behaviour == "down":
            raise httpx.ConnectError("down")
        verdict, reason = behaviour if isinstance(behaviour, tuple) else (behaviour, "test")
        return httpx.Response(200, json={"requestId": body["requestId"], "detectorId": name,
                                         "verdict": verdict, "confidence": 0.0 if verdict == "abstain" else 0.8,
                                         "problemType": OWN_TYPE[name] if verdict == "problem" else None,
                                         "reason": reason, "latencyMs": 1})

    def _alert(self, report: dict) -> httpx.Response:
        if self.alert_down:
            raise httpx.ConnectError("down")
        self.reports.append(report)
        if self.alert_status != 200:
            return httpx.Response(self.alert_status, json={"error": {"code": "unavailable"}})
        if report["problemType"] == self.alert_fails_for:
            return httpx.Response(503, json={"error": {"code": "unavailable"}})
        iid = f"{report['travellerId']}#{report['tripId']}#{report['problemType']}#{report['stepIndex']}"
        created = iid not in self.incidents
        self.incidents.add(iid)
        return httpx.Response(200, json={"incidentId": iid, "created": created})

    @property
    def asked(self) -> list[str]:
        return [name for name, _ in self.detector_calls]


def settings(**overrides) -> Settings:
    # The general tests use adaptive mode with the fixed pair (route-corridor + dwell-time), so k = 2
    # at normal risk; fixed4 (the default) and the step-aware pairs have their own tests below.
    base = dict(redundancy_mode="adaptive", pair_strategy="fixed", detector_urls=parse_urls(""),
                detector_timeout_s=0.1, quorum=2, alert_url="http://alert-service:8000",
                decided_ttl_s=86400, key_ttl_s=86400, decisions_per_traveller=50, decisions_recent=200)
    return Settings(**{**base, **overrides})


def new_redis(hashes=None, keys=None, connected=True):
    server = fakeredis.FakeServer()
    server.connected = connected
    redis = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)

    async def seed():
        for key, mapping in (hashes or {}).items():
            await redis.hset(key, mapping=mapping)
        for key, value in (keys or {}).items():
            await redis.set(key, value)
    if connected:
        asyncio.run(seed())
    return redis


def process_all(world: World, checkins, redis=None, **overrides):
    """Process check-ins in order. Returns (results, redis state)."""
    redis = redis if redis is not None else new_redis()
    s = settings(**overrides)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(world.handler)) as http:
            p = Pipeline(redis, DetectorClient(http, s.detector_urls, s.detector_timeout_s),
                         DecisionRecorder(redis, http, s), s)
            results = [await p.process(c) for c in checkins]
        state = {
            "risk": await redis.hgetall(risk_key("U-042")),
            "decisions": [json.loads(x) for x in await redis.lrange(traveller_decisions_key("U-042"), 0, -1)],
            "recent": [json.loads(x) for x in await redis.lrange(RECENT_DECISIONS_KEY, 0, -1)],
            "decided": {k: (await redis.get(k), await redis.ttl(k)) for k in await redis.keys("decided:*")},
        }
        return results, state

    return asyncio.run(go())


def process(world: World, redis=None, checkin=CHECKIN, **overrides):
    [result], state = process_all(world, [checkin], redis, **overrides)
    return result, state


def seq(n: int) -> dict:
    return {**CHECKIN, "seq": n}


ELEVATED = {"risk:U-042": {"level": "elevated", "score": "0.5"}}


# --- normal path ----------------------------------------------------------------------------

def test_normal_risk_asks_the_diverse_pair_once():
    world = World()
    result, state = process(world)
    assert sorted(world.asked) == ["dwell-time", "route-corridor"]
    assert result.outcome == "decided" and result.k == 2 and not result.escalated
    assert result.decision == {"decision": "normal", "problemType": None, "confidence": 1.0, "reason": None,
                               "riskLevel": "normal"}
    assert world.reports == []
    assert state["risk"]["level"] == "normal" and state["risk"]["detectorsUsed"] == "2"
    assert len(state["decisions"]) == 1 and len(state["recent"]) == 1


def test_marks_the_checkin_as_decided_for_a_day():
    _, state = process(World())
    value, ttl = state["decided"][decided_key("T-001", 17)]
    assert value == "1" and 0 < ttl <= 86400


def test_detectors_receive_a_contract_shaped_request():
    world = World()
    process(world)
    for _, body in world.detector_calls:
        assert set(body) == DETECTOR_REQUEST_FIELDS
        assert body["requestId"] == "T-001#17"
        assert body["riskLevel"] == "normal"
        assert body["checkin"] == CHECKIN


# --- problems are reported ---------------------------------------------------------------------------

def test_problem_is_reported_to_the_alert_service_and_raises_risk():
    world = World(detector_votes={"route-corridor": "problem"})
    result, state = process(world)
    assert result.decision["decision"] == "problem" and result.decision["problemType"] == "off_route"
    assert result.decision["riskLevel"] == "elevated"
    [report] = world.reports
    assert set(report) == REPORT_FIELDS
    assert report["reportId"] == "T-001#17" and report["detectorsUsed"] == 2
    record = state["decisions"][0]
    assert record["incidentId"] == "U-042#T-001#off_route#2" and record["alertCreated"] is True


def test_two_problem_types_at_once_are_both_reported():
    # Walking sideways off the road can look like the wrong direction too: the caregiver gets both.
    world = World(detector_votes={"route-corridor": "problem", "heading-speed": "problem"})
    result, state = process(world, redundancy_mode="fixed4")
    assert sorted(r["problemType"] for r in world.reports) == ["off_route", "wrong_direction"]
    assert world.incidents == {"U-042#T-001#off_route#2", "U-042#T-001#wrong_direction#2"}
    record = state["decisions"][0]
    assert [a["problemType"] for a in record["alerts"]] == ["off_route", "wrong_direction"]   # equal: alphabetical
    assert record["incidentId"] == "U-042#T-001#off_route#2"
    assert result.decision["riskLevel"] == "elevated"


def test_a_failed_second_report_retries_the_checkin_without_a_second_incident():
    world = World(detector_votes={"route-corridor": "problem", "heading-speed": "problem"},
                  alert_fails_for="wrong_direction")
    redis = new_redis()
    with pytest.raises(httpx.HTTPStatusError):
        process(world, redis, redundancy_mode="fixed4")
    assert not asyncio.run(redis.exists(decided_key("T-001", 17)))   # not decided: SQS will retry
    world.alert_fails_for = None
    result, state = process(world, redis, redundancy_mode="fixed4")
    assert result.outcome == "decided"
    assert world.incidents == {"U-042#T-001#off_route#2", "U-042#T-001#wrong_direction#2"}
    assert [a["created"] for a in state["decisions"][0]["alerts"]] == [False, True]   # off_route already existed
    assert len(state["decisions"]) == 1


def test_one_problem_from_the_pair_alerts_at_once():
    # Any detector that checks the type saying problem is enough: no second opinion first.
    world = World(detector_votes={"route-corridor": "problem"})
    result, _ = process(world)
    assert result.k == 2 and not result.escalated
    assert len(world.reports) == 1


def test_decision_record_has_what_the_dashboard_needs():
    world = World(detector_votes={"route-corridor": "problem"})
    _, state = process(world)
    record = state["decisions"][0]
    assert record["requestId"] == "T-001#17" and record["seq"] == 17 and record["stepIndex"] == 2
    assert record["k"] == 2 and record["escalated"] is False and record["situation"] == "fixed"
    assert record["votes"][0] == {"detectorId": "route-corridor", "verdict": "problem",
                                  "problemType": "off_route", "confidence": 0.8,
                                  "latencyMs": record["votes"][0]["latencyMs"], "reason": "test"}
    assert record["riskBefore"] == {"level": "normal", "score": 0.0}
    assert record["riskAfter"] == {"level": "elevated", "score": 0.5}
    assert state["recent"][0] == record


# --- risk and modes ------------------------------------------------------------------------------

def test_elevated_risk_asks_every_detector():
    world = World()
    result, _ = process(world, new_redis(hashes=ELEVATED))
    assert sorted(world.asked) == ["dwell-time", "heading-speed", "route-corridor", "route-progress"]
    assert result.k == 4 and not result.escalated
    assert world.detector_calls[0][1]["riskLevel"] == "elevated"


def test_fixed2_ignores_elevated_risk():
    world = World()
    process(world, new_redis(hashes=ELEVATED), redundancy_mode="fixed2")
    assert len(world.asked) == 2


def test_fixed4_asks_every_detector_at_normal_risk():
    world = World()
    result, _ = process(world, redundancy_mode="fixed4")
    assert len(world.asked) == 4 and result.situation is None


def test_fixed4_too_few_votes_is_inconclusive_without_asking_again():
    world = World(detector_votes={"route-corridor": "abstain", "heading-speed": "abstain",
                                  "dwell-time": "abstain"})
    result, state = process(world, redundancy_mode="fixed4")
    assert sorted(world.asked) == ["dwell-time", "heading-speed", "route-corridor", "route-progress"]
    assert result.k == 4 and not result.escalated
    assert result.decision["decision"] == "inconclusive"
    assert state["decided"]   # still marked decided: a redelivery isn't asked again


def test_risk_over_a_journey():
    redis = new_redis()
    [problem], _ = process_all(World(detector_votes={"route-corridor": "problem"}), [seq(17)], redis)
    calm, state = process_all(World(), [seq(18), seq(19), seq(20)], redis)
    assert problem.decision["riskLevel"] == "elevated"
    # Hysteresis: back to normal only after 3 calm check-ins in a row.
    assert [r.decision["riskLevel"] for r in calm] == ["elevated", "elevated", "normal"]
    assert state["risk"]["calmStreak"] == "3"


def test_start_of_trip_inconclusive_leaves_risk_unchanged():
    # The detectors need history before they can judge (their own reasons), so nobody can call it.
    warming_up = ("abstain", "only 1 usable check-in(s), need 3")
    world = World(detector_votes={d: warming_up for d in HOSTS.values()} | {"dwell-time": "normal"})
    result, state = process(world)
    assert result.decision["decision"] == "inconclusive" and result.decision["riskLevel"] == "normal"
    assert result.k == 4 and result.escalated                        # asked everyone first
    assert world.reports == []
    assert state["decisions"][0]["decision"] == "inconclusive"         # still recorded for the dashboard
    assert state["decisions"][0]["riskAfter"] == {"level": "normal", "score": 0.0}


def test_inconclusive_because_detectors_failed_raises_risk_without_alert():
    world = World(detector_votes={"dwell-time": "timeout", "heading-speed": "down", "route-progress": "timeout"})
    result, state = process(world)
    assert result.decision["decision"] == "inconclusive"
    assert world.reports == []
    assert state["risk"]["level"] == "elevated"


def test_decision_lists_are_trimmed():
    _, state = process_all(World(), [seq(s) for s in range(1, 6)], decisions_per_traveller=3, decisions_recent=4)
    assert [d["seq"] for d in state["decisions"]] == [5, 4, 3]
    assert len(state["recent"]) == 4


# --- duplicates ------------------------------------------------------------------------------------

def test_already_decided_checkin_is_skipped():
    world = World()
    result, _ = process(world, new_redis(keys={decided_key("T-001", 17): "1"}))
    assert result.outcome == "duplicate"
    assert world.detector_calls == [] and world.reports == []


def test_processing_the_same_checkin_twice_decides_once():
    world = World(detector_votes={"route-corridor": "problem"})
    (first, second), state = process_all(world, [CHECKIN, CHECKIN])
    assert (first.outcome, second.outcome) == ("decided", "duplicate")
    assert len(world.reports) == 1
    assert len(state["decisions"]) == 1
    assert state["risk"]["score"] == "0.5000"   # not raised twice


def test_different_checkins_are_both_decided():
    results, state = process_all(World(), [seq(17), seq(18)])
    assert [r.outcome for r in results] == ["decided", "decided"]
    assert [d["seq"] for d in state["decisions"]] == [18, 17]   # newest first


# --- escalation --------------------------------------------------------------------------------------

def test_too_few_real_votes_asks_the_rest_before_deciding():
    world = World(detector_votes={"dwell-time": "timeout"})
    result, state = process(world)
    assert result.k == 4 and result.escalated
    assert len(state["decisions"]) == 1 and state["decisions"][0]["k"] == 4


def test_escalated_votes_keep_the_abstain():
    world = World(detector_votes={"dwell-time": "down"})
    _, state = process(world)
    dwell = next(v for v in state["decisions"][0]["votes"] if v["detectorId"] == "dwell-time")
    assert dwell["verdict"] == "abstain" and dwell["reason"] == "error: ConnectError"


def test_answers_without_weight_ask_the_rest_and_decide_again():
    # Both of the pair are known to be faulty (reliability 0): their normals can't call it.
    world = World()
    redis = new_redis(hashes={WEIGHTS_KEY: {"route-corridor": "0", "dwell-time": "0"}})
    result, _ = process(world, redis)
    assert sorted(world.asked) == ["dwell-time", "heading-speed", "route-corridor", "route-progress"]
    assert result.escalated and result.k == 4
    assert result.decision["decision"] == "normal"


def test_no_one_left_to_ask_records_the_inconclusive():
    redis = new_redis(hashes={**ELEVATED, WEIGHTS_KEY: {d: "0" for d in HOSTS.values()}})
    world = World()
    result, state = process(world, redis)
    assert result.k == 4 and not result.escalated
    assert result.decision["decision"] == "inconclusive" and result.decision["reason"] == "no_weight"
    assert state["decisions"][0]["decision"] == "inconclusive"


def test_no_detector_is_asked_twice():
    world = World(detector_votes={"dwell-time": "timeout"})
    process(world)
    assert sorted(world.asked) == ["dwell-time", "heading-speed", "route-corridor", "route-progress"]


def test_zero_reliability_silences_a_faulty_detector():
    world = World(detector_votes={"route-corridor": "problem"})
    plain, _ = process(world, redundancy_mode="fixed4")
    silenced, _ = process(World(detector_votes={"route-corridor": "problem"}),
                          new_redis(hashes={WEIGHTS_KEY: {"route-corridor": "0"}}), redundancy_mode="fixed4")
    assert plain.decision["decision"] == "problem"
    assert silenced.decision["decision"] == "normal"


# --- failures: raise so the message is retried -----------------------------------------------------------

@pytest.mark.parametrize("world", [World(detector_votes={"route-corridor": "problem"}, alert_status=503),
                                   World(detector_votes={"route-corridor": "problem"}, alert_down=True)])
def test_alert_service_failure_raises_and_changes_nothing(world):
    redis = new_redis()
    with pytest.raises(httpx.HTTPError):
        process(world, redis)

    async def state():
        return await redis.keys("*")
    assert asyncio.run(state()) == []   # not decided, no risk, no record: the retry starts clean


def test_after_an_alert_failure_the_retry_decides_once():
    redis = new_redis()
    world = World(detector_votes={"route-corridor": "problem"}, alert_status=503)
    with pytest.raises(httpx.HTTPStatusError):
        process(world, redis)
    world.alert_status = 200
    result, state = process(world, redis)
    assert result.decision["decision"] == "problem"
    assert len(state["decisions"]) == 1 and len(world.incidents) == 1


def test_redis_down_raises():
    with pytest.raises(Exception):
        process(World(), new_redis(connected=False))


@pytest.mark.parametrize("bad", [
    {k: v for k, v in CHECKIN.items() if k != "tripId"},
    {k: v for k, v in CHECKIN.items() if k != "travellerId"},
    {**CHECKIN, "seq": "seventeen"},
])
def test_invalid_checkin_raises_before_calling_anyone(bad):
    world = World()
    with pytest.raises(ValueError):
        process(world, checkin=bad)
    assert world.detector_calls == [] and world.reports == []


# --- step-aware pair (PAIR_STRATEGY=step) ------------------------------------------------------

ROUTE = json.loads((CONTRACTS / "route.json").read_text())   # step 0 walk, step 1 bus
ROUTE_KEY = {"trip:T-001:route": json.dumps({"steps": ROUTE["steps"], "path": ROUTE["path"]})}


def at_step(step, speed=1.3):
    return {**CHECKIN, "stepIndex": step, "speedMps": speed}


@pytest.mark.parametrize("step, speed, pair, situation", [
    (0, 1.3, ["heading-speed", "route-corridor"], "walking"),
    (1, 0.2, ["dwell-time", "route-corridor"], "waiting"),
    (1, None, ["dwell-time", "route-corridor"], "waiting"),       # no speed: assume still at the stop
    (1, 9.0, ["route-corridor", "route-progress"], "riding"),
])
def test_pair_fits_the_journey_step(step, speed, pair, situation):
    world = World()
    result, state = process(world, new_redis(keys=ROUTE_KEY), checkin=at_step(step, speed), pair_strategy="step")
    assert sorted(world.asked) == pair
    assert result.situation == situation and result.k == 2
    assert state["decisions"][0]["situation"] == situation


@pytest.mark.parametrize("keys, checkin", [
    ({}, at_step(0)),                                            # no route in Redis
    (ROUTE_KEY, at_step(7)),                                     # step index beyond the route
    (ROUTE_KEY, {**at_step(0), "stepIndex": None}),
    ({"trip:T-001:route": "{not json"}, at_step(0)),
])
def test_unknown_step_falls_back_to_route_corridor_and_route_progress(keys, checkin):
    world = World()
    result, _ = process(world, new_redis(keys=keys), checkin=checkin, pair_strategy="step")
    assert sorted(world.asked) == ["route-corridor", "route-progress"]
    assert result.situation == "unknown step"


def test_elevated_risk_still_asks_everyone_and_skips_the_route_lookup():
    world = World()
    result, _ = process(world, new_redis(hashes=ELEVATED), checkin=at_step(0), pair_strategy="step")
    assert result.k == 4 and result.situation is None


def test_fixed_strategy_ignores_the_step():
    world = World()
    result, _ = process(world, new_redis(keys=ROUTE_KEY), checkin=at_step(1, 9.0), pair_strategy="fixed")
    assert sorted(world.asked) == ["dwell-time", "route-corridor"]
    assert result.situation == "fixed"


def test_fixed2_also_uses_the_step_pair():
    world = World()
    process(world, new_redis(hashes=ELEVATED, keys=ROUTE_KEY), checkin=at_step(0), redundancy_mode="fixed2",
            pair_strategy="step")
    assert sorted(world.asked) == ["heading-speed", "route-corridor"]


def test_custom_waiting_speed():
    world = World()
    result, _ = process(world, new_redis(keys=ROUTE_KEY), checkin=at_step(1, 1.5), pair_strategy="step",
                        waiting_below_mps=1.0)
    assert result.situation == "riding"


def test_unknown_strategy_is_rejected():
    with pytest.raises(ValueError):
        process(World(), new_redis(keys=ROUTE_KEY), checkin=at_step(0), pair_strategy="random")
