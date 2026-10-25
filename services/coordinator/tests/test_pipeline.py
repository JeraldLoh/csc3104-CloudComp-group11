"""Pipeline with a fake Redis and fake detectors + Aggregator behind httpx.MockTransport."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.detectors import DetectorClient
from app.pipeline import Pipeline, decided_key
from app.selection import parse_urls

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "examples"
CHECKIN = json.loads((CONTRACTS / "checkin.json").read_text())        # T-001, seq 17, U-042
DETECTOR_REQUEST_FIELDS = set(json.loads((CONTRACTS / "detector-request.json").read_text()))
HOSTS = {"detector-route": "route-corridor", "detector-heading": "heading-speed",
         "detector-dwell": "dwell-time", "detector-ml": "ml-anomaly"}


class FakeRedis:
    def __init__(self, hashes=None, keys=None, down=False):
        self.hashes = hashes or {}
        self.keys = dict(keys or {})
        self.ttl = {}
        self.down = down

    def _check(self):
        if self.down:
            raise ConnectionError("redis down")

    async def exists(self, key):
        self._check()
        return int(key in self.keys)

    async def hget(self, key, field):
        self._check()
        return self.hashes.get(key, {}).get(field)

    async def set(self, key, value, ex=None):
        self._check()
        self.keys[key] = value
        self.ttl[key] = ex


class World:
    """Fake detectors and Aggregator. Detectors answer `detector_votes[name]` (or a callable
    raising/returning a Response); the Aggregator answers the next item of `agg_replies`."""

    def __init__(self, detector_votes=None, agg_replies=None):
        self.detector_votes = {"route-corridor": "normal", "heading-speed": "normal",
                               "dwell-time": "normal", "ml-anomaly": "normal", **(detector_votes or {})}
        self.agg_replies = list(agg_replies or [{"decision": "normal", "problemType": None, "confidence": 1.0}])
        self.detector_calls = []   # (detectorId, request body)
        self.agg_calls = []        # request bodies

    async def handler(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if req.url.host == "aggregator":
            self.agg_calls.append(body)
            reply = self.agg_replies.pop(0) if len(self.agg_replies) > 1 else self.agg_replies[0]
            return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)
        name = HOSTS[req.url.host]
        self.detector_calls.append((name, body))
        behaviour = self.detector_votes[name]
        if behaviour == "timeout":
            await asyncio.sleep(1)
        if behaviour == "down":
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"requestId": body["requestId"], "detectorId": name,
                                         "verdict": behaviour, "confidence": 0.8,
                                         "problemType": "off_route" if behaviour == "problem" else None,
                                         "reason": "test", "latencyMs": 1})

    @property
    def asked(self) -> list[str]:
        return [name for name, _ in self.detector_calls]


def settings(**overrides) -> Settings:
    base = dict(redundancy_mode="adaptive", detector_urls=parse_urls(""), detector_timeout_s=0.1,
                quorum=2, aggregator_url="http://aggregator:8000", decided_ttl_s=86400)
    return Settings(**{**base, **overrides})


def process(world: World, redis: FakeRedis | None = None, checkin=CHECKIN, **overrides):
    redis = redis if redis is not None else FakeRedis()
    s = settings(**overrides)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(world.handler)) as http:
            p = Pipeline(redis, http, DetectorClient(http, s.detector_urls, s.detector_timeout_s), s)
            return await p.process(checkin)

    return asyncio.run(go()), redis


ELEVATED = {"risk:U-042": {"level": "elevated"}}
NEEDS_MORE = {"decision": "inconclusive", "problemType": None, "confidence": 0.0,
              "needsMoreVotes": True, "reason": "split"}
PROBLEM = {"decision": "problem", "problemType": "off_route", "confidence": 0.9}


# --- normal path ----------------------------------------------------------------------------

def test_normal_risk_asks_the_diverse_pair_once():
    world = World()
    result, redis = process(world)
    assert sorted(world.asked) == ["dwell-time", "route-corridor"]
    assert len(world.agg_calls) == 1
    assert result.outcome == "decided" and result.k == 2 and not result.escalated
    assert result.decision["decision"] == "normal"


def test_marks_the_checkin_as_decided_for_a_day():
    _, redis = process(World())
    assert redis.keys[decided_key("T-001", 17)] == "1"
    assert redis.ttl[decided_key("T-001", 17)] == 86400


def test_detectors_receive_a_contract_shaped_request():
    world = World()
    process(world)
    for _, body in world.detector_calls:
        assert set(body) == DETECTOR_REQUEST_FIELDS
        assert body["requestId"] == "T-001#17"
        assert body["riskLevel"] == "normal"
        assert body["checkin"] == CHECKIN


def test_aggregator_receives_votes_and_context():
    world = World(detector_votes={"route-corridor": "problem"}, agg_replies=[PROBLEM])
    result, _ = process(world)
    [body] = world.agg_calls
    assert set(body) == {"requestId", "checkin", "votes", "riskLevel", "escalated"}
    assert body["requestId"] == "T-001#17"
    assert body["checkin"] == CHECKIN
    assert sorted(v["detectorId"] for v in body["votes"]) == ["dwell-time", "route-corridor"]
    assert body["riskLevel"] == "normal" and body["escalated"] is False
    assert result.decision == PROBLEM


# --- risk and modes ------------------------------------------------------------------------------

def test_elevated_risk_asks_all_four():
    world = World()
    result, _ = process(world, FakeRedis(hashes=ELEVATED))
    assert sorted(world.asked) == ["dwell-time", "heading-speed", "ml-anomaly", "route-corridor"]
    assert result.k == 4 and not result.escalated
    assert world.detector_calls[0][1]["riskLevel"] == "elevated"


def test_fixed2_ignores_elevated_risk():
    world = World()
    process(world, FakeRedis(hashes=ELEVATED), redundancy_mode="fixed2")
    assert len(world.asked) == 2


def test_fixed4_asks_all_four_at_normal_risk():
    world = World()
    process(world, redundancy_mode="fixed4")
    assert len(world.asked) == 4


# --- duplicates ------------------------------------------------------------------------------------

def test_already_decided_checkin_is_skipped():
    world = World()
    redis = FakeRedis(keys={decided_key("T-001", 17): "1"})
    result, _ = process(world, redis)
    assert result.outcome == "duplicate"
    assert world.detector_calls == [] and world.agg_calls == []


def test_processing_the_same_checkin_twice_decides_once():
    world = World()
    redis = FakeRedis()
    first, _ = process(world, redis)
    second, _ = process(world, redis)
    assert (first.outcome, second.outcome) == ("decided", "duplicate")
    assert len(world.agg_calls) == 1


# --- escalation --------------------------------------------------------------------------------------

def test_too_few_real_votes_escalates_before_asking_the_aggregator():
    world = World(detector_votes={"dwell-time": "timeout"})
    result, _ = process(world)
    assert len(world.agg_calls) == 1                       # one round trip, already with 4 votes
    assert len(world.agg_calls[0]["votes"]) == 4
    assert world.agg_calls[0]["escalated"] is True
    assert result.k == 4 and result.escalated


def test_escalated_votes_keep_the_abstain():
    world = World(detector_votes={"dwell-time": "down"})
    process(world)
    dwell = next(v for v in world.agg_calls[0]["votes"] if v["detectorId"] == "dwell-time")
    assert dwell["verdict"] == "abstain"


def test_close_call_asks_the_rest_and_resubmits():
    world = World(detector_votes={"route-corridor": "problem"}, agg_replies=[NEEDS_MORE, PROBLEM])
    result, _ = process(world)
    assert len(world.agg_calls) == 2
    assert len(world.agg_calls[0]["votes"]) == 2
    assert len(world.agg_calls[1]["votes"]) == 4
    assert world.agg_calls[1]["escalated"] is True
    assert sorted(world.asked) == ["dwell-time", "heading-speed", "ml-anomaly", "route-corridor"]
    assert result.decision == PROBLEM and result.k == 4 and result.escalated


def test_no_resubmit_when_everyone_was_already_asked():
    world = World(agg_replies=[NEEDS_MORE])
    result, _ = process(world, FakeRedis(hashes=ELEVATED))
    assert len(world.agg_calls) == 1
    assert result.decision == NEEDS_MORE
    assert result.outcome == "decided"


def test_no_detector_is_asked_twice():
    world = World(detector_votes={"dwell-time": "timeout"}, agg_replies=[NEEDS_MORE, PROBLEM])
    process(world)
    assert sorted(world.asked) == ["dwell-time", "heading-speed", "ml-anomaly", "route-corridor"]


# --- failures: raise so the message is retried -----------------------------------------------------------

def test_aggregator_error_raises_and_leaves_it_undecided():
    world = World(agg_replies=[httpx.Response(503, json={"error": {"code": "unavailable"}})])
    with pytest.raises(httpx.HTTPStatusError):
        process(world, redis := FakeRedis())
    assert decided_key("T-001", 17) not in redis.keys


def test_aggregator_unreachable_raises():
    class Down(World):
        async def handler(self, req):
            if req.url.host == "aggregator":
                raise httpx.ConnectError("down")
            return await super().handler(req)
    with pytest.raises(httpx.ConnectError):
        process(Down())


def test_redis_down_raises():
    with pytest.raises(ConnectionError):
        process(World(), FakeRedis(down=True))


@pytest.mark.parametrize("bad", [
    {k: v for k, v in CHECKIN.items() if k != "tripId"},
    {k: v for k, v in CHECKIN.items() if k != "travellerId"},
    {**CHECKIN, "seq": "seventeen"},
])
def test_invalid_checkin_raises_before_calling_anyone(bad):
    world = World()
    with pytest.raises(ValueError):
        process(world, checkin=bad)
    assert world.detector_calls == [] and world.agg_calls == []
