"""What the Aggregator does with one set of votes (POST /internal/votes).

    1. Already answered this requestId?     return the saved answer (SQS redelivery / retry)
    2. Rule-based decision                  voting.py: any detector that checks it says problem
    3. Too few votes to call it normal?     needsMoreVotes: answer now, change nothing
    4. Problem?                             problem-report.json -> Alert Service
    5. Update risk:{travellerId}            risk.py              \\  one Redis
    6. Save the decision record             for the dashboard    |  transaction
    7. Save the answer under requestId                           /

Step 4 runs before anything is written. If the Alert Service fails, this raises with no
state changed and the Coordinator's retry starts clean. If step 4 succeeded but the Redis
write fails, the retry sends the report again, which the Alert Service deduplicates.
"""
import json
import logging
from datetime import datetime, timezone

import httpx

from .config import Settings
from .reports import build_problem_report, iso_z
from .risk import Risk, detectors_failed, update_risk
from .voting import Decision, aggregate

log = logging.getLogger("aggregator")


def risk_key(traveller_id: str) -> str:
    return f"risk:{traveller_id}"


def answer_key(request_id: str) -> str:
    return f"aggregated:{request_id}"


def traveller_decisions_key(traveller_id: str) -> str:
    return f"decisions:{traveller_id}"


RECENT_DECISIONS_KEY = "decisions:recent"
WEIGHTS_KEY = "detector:weights"


def parse_weights(raw: dict) -> dict[str, float]:
    """detector:weights hash -> {detectorId: weight}; unreadable entries are skipped (weight 1.0)."""
    weights = {}
    for k, v in (raw or {}).items():
        try:
            weights[k] = float(v)
        except (TypeError, ValueError):
            log.warning("ignoring bad weight %r for %s", v, k)
    return weights


def answer(d: Decision, risk_level: str | None) -> dict:
    """Reply to the Coordinator: the contract's decision/problemType/confidence, plus
    needsMoreVotes and reason (close calls) and the traveller's risk level afterwards."""
    return {
        "decision": d.decision,
        "problemType": d.problem_type,
        "confidence": round(d.confidence, 2),
        "needsMoreVotes": d.needs_more_votes,
        "reason": d.reason,
        "riskLevel": risk_level,
    }


def decision_record(request_id: str, checkin: dict, votes: list[dict], d: Decision, escalated: bool,
                    risk_before: Risk, risk_after: Risk, incident: dict | None, now: datetime,
                    situation: str | None = None) -> dict:
    """One entry of decisions:{travellerId}: what the dashboard and the experiment read."""
    return {
        "requestId": request_id,
        "tripId": checkin.get("tripId"),
        "travellerId": checkin.get("travellerId"),
        "seq": checkin.get("seq"),
        "stepIndex": checkin.get("stepIndex"),
        "decidedAt": iso_z(now),
        "k": len(votes),
        "escalated": escalated,
        "situation": situation,
        "votes": [{"detectorId": v.get("detectorId"), "verdict": v.get("verdict"),
                   "confidence": v.get("confidence"), "latencyMs": v.get("latencyMs"),
                   "reason": v.get("reason")} for v in votes],
        "decision": d.decision,
        "reason": d.reason,
        "problemType": d.problem_type,
        "problemScore": None if d.problem_score is None else round(d.problem_score, 4),
        "confidence": round(d.confidence, 4),
        "riskBefore": {"level": risk_before.level, "score": round(risk_before.score, 4)},
        "riskAfter": {"level": risk_after.level, "score": round(risk_after.score, 4)},
        "incidentId": incident.get("incidentId") if incident else None,
        "alertCreated": incident.get("created") if incident else None,
    }


class AggregatorService:
    def __init__(self, redis, http: httpx.AsyncClient, s: Settings):
        self.redis = redis
        self.http = http
        self.s = s

    async def _report_problem(self, report: dict) -> dict:
        resp = await self.http.post(f"{self.s.alert_url}/internal/problems", json=report,
                                    timeout=self.s.alert_timeout_s)
        resp.raise_for_status()
        return resp.json()

    async def handle(self, request_id: str, checkin: dict, votes: list[dict], escalated: bool = False,
                     now: datetime | None = None, situation: str | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        traveller_id = checkin["travellerId"]

        saved = await self.redis.get(answer_key(request_id))
        if saved:
            log.info("repeat of %s: returning the saved answer", request_id)
            return json.loads(saved)

        weights = parse_weights(await self.redis.hgetall(WEIGHTS_KEY))
        d = aggregate(votes, weights, self.s.quorum)
        if d.needs_more_votes:
            log.info("%s needs more votes (%s, %d counted)", request_id, d.reason, d.counted)
            return answer(d, None)

        incident = None
        if d.decision == "problem":
            incident = await self._report_problem(build_problem_report(request_id, checkin, votes, d, now))

        before = Risk.from_hash(await self.redis.hgetall(risk_key(traveller_id)))
        after = update_risk(before, d, self.s.risk, failed=detectors_failed(votes))
        reply = answer(d, after.level)
        record = json.dumps(decision_record(request_id, checkin, votes, d, escalated, before, after, incident, now,
                                            situation))
        ttl = self.s.key_ttl_s

        tx = self.redis.pipeline(transaction=True)
        tx.hset(risk_key(traveller_id), mapping={**after.to_hash(), "detectorsUsed": str(len(votes)),
                                                 "updatedAt": iso_z(now)})
        tx.expire(risk_key(traveller_id), ttl)
        tx.lpush(traveller_decisions_key(traveller_id), record)
        tx.ltrim(traveller_decisions_key(traveller_id), 0, self.s.decisions_per_traveller - 1)
        tx.expire(traveller_decisions_key(traveller_id), ttl)
        tx.lpush(RECENT_DECISIONS_KEY, record)
        tx.ltrim(RECENT_DECISIONS_KEY, 0, self.s.decisions_recent - 1)
        tx.set(answer_key(request_id), json.dumps(reply), ex=ttl)
        await tx.execute()

        log.info("%s -> %s (k=%d, risk %s -> %s%s)", request_id, d.decision, len(votes), before.level,
                 after.level, f", incident {incident['incidentId']}" if incident else "")
        return reply
