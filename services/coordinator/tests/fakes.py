"""The world the pipeline tests run in: an in-memory Redis (fakeredis), fake detectors and a fake
Alert Service behind httpx.MockTransport. Decisions are real (decision.py, voting.py).
Shared by test_pipeline.py (fixed4, the default) and test_adaptive.py (the experiment)."""
import asyncio
import json
from pathlib import Path

import fakeredis
import httpx

from app.config import Settings
from app.decision import RECENT_DECISIONS_KEY, DecisionRecorder, risk_key, traveller_decisions_key
from app.detectors import DetectorClient, parse_urls
from app.pipeline import Pipeline

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "examples"
CHECKIN = json.loads((CONTRACTS / "checkin.json").read_text())        # T-001, seq 17, U-042, step 2
DETECTOR_REQUEST_FIELDS = set(json.loads((CONTRACTS / "detector-request.json").read_text()))
REPORT_FIELDS = set(json.loads((CONTRACTS / "problem-report.json").read_text()))
ROUTE = json.loads((CONTRACTS / "route.json").read_text())   # step 0 walk, step 1 bus
ROUTE_KEY = {"trip:T-001:route": json.dumps({"steps": ROUTE["steps"], "path": ROUTE["path"]})}
HOSTS = {"detector-route": "route-corridor", "detector-heading": "heading-speed",
         "detector-dwell": "dwell-time", "detector-progress": "route-progress"}
OWN_TYPE = {"route-corridor": "off_route", "heading-speed": "wrong_direction",
            "dwell-time": "long_dwell", "route-progress": "off_route"}
ALL_FOUR = ["dwell-time", "heading-speed", "route-corridor", "route-progress"]   # sorted
ELEVATED = {"risk:U-042": {"level": "elevated", "score": "0.5"}}


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
    """fixed4, the default, unless a test says otherwise (test_adaptive.py does)."""
    base = dict(redundancy_mode="fixed4", detector_urls=parse_urls(""),
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
