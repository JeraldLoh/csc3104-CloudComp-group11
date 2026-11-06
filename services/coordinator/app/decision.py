"""What happens once the votes for a check-in are in.

    decide()   rule-based decision          voting.py, with detector:weights from Redis
    record()   1. Problem?                  problem-report.json -> Alert Service, one per
                                            problem type raised (strongest first)
               2. Update risk:{travellerId}  risk.py              \\  one Redis
               3. Save the decision record   for the dashboard    |  transaction
               4. Mark tripId + seq decided  repeats are skipped  /

Step 1 runs before anything is written. If the Alert Service fails, record() raises with no
Redis state changed: the message stays on the queue and is retried. Reports already sent (an
earlier type, or all of them if the Redis write failed) are sent again, which the Alert
Service deduplicates by incident id. Steps 2-4 are one transaction, so a check-in is either fully decided or not
at all: risk is never raised twice for the same check-in.
"""
import json
import logging
from datetime import datetime, timezone

import httpx

from .config import Settings
from .reports import build_problem_reports, iso_z
from .risk import Risk, detectors_failed, update_risk
from .voting import Decision, aggregate

log = logging.getLogger("coordinator")


def decided_key(trip_id: str, seq: int) -> str:
    return f"decided:{trip_id}:{seq}"


def risk_key(traveller_id: str) -> str:
    return f"risk:{traveller_id}"


def traveller_decisions_key(traveller_id: str) -> str:
    return f"decisions:{traveller_id}"


RECENT_DECISIONS_KEY = "decisions:recent"
WEIGHTS_KEY = "detector:weights"


def parse_weights(raw: dict) -> dict[str, float]:
    """detector:weights hash -> {detectorId: weight}; unreadable entries are skipped."""
    weights = {}
    for k, v in (raw or {}).items():
        try:
            weights[k] = float(v)
        except (TypeError, ValueError):
            log.warning("ignoring bad weight %r for %s", v, k)
    return weights


def summary(d: Decision, risk_level: str | None) -> dict:
    """The decision as the pipeline reports it: contract fields plus why and the risk afterwards."""
    return {
        "decision": d.decision,
        "problemType": d.problem_type,
        "confidence": round(d.confidence, 2),
        "reason": d.reason,
        "riskLevel": risk_level,
    }


def decision_record(request_id: str, checkin: dict, votes: list[dict], d: Decision, escalated: bool,
                    risk_before: Risk, risk_after: Risk, alerts: list[dict], now: datetime,
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
                   "problemType": v.get("problemType"), "confidence": v.get("confidence"), "latencyMs": v.get("latencyMs"),
                   "reason": v.get("reason")} for v in votes],
        "decision": d.decision,
        "reason": d.reason,
        "problemType": d.problem_type,
        "problemScore": None if d.problem_score is None else round(d.problem_score, 4),
        "confidence": round(d.confidence, 4),
        "riskBefore": {"level": risk_before.level, "score": round(risk_before.score, 4)},
        "riskAfter": {"level": risk_after.level, "score": round(risk_after.score, 4)},
        # The strongest problem's incident, as before; `alerts` has one entry per type reported.
        "incidentId": alerts[0]["incidentId"] if alerts else None,
        "alertCreated": alerts[0]["created"] if alerts else None,
        "alerts": alerts,
    }


class DecisionRecorder:
    def __init__(self, redis, http: httpx.AsyncClient, s: Settings):
        self.redis = redis
        self.http = http
        self.s = s

    async def decide(self, votes: list[dict]) -> Decision:
        weights = parse_weights(await self.redis.hgetall(WEIGHTS_KEY))
        return aggregate(votes, weights, self.s.quorum)

    async def _report_problem(self, report: dict) -> dict:
        resp = await self.http.post(f"{self.s.alert_url}/internal/problems", json=report,
                                    timeout=self.s.alert_timeout_s)
        resp.raise_for_status()
        return resp.json()

    async def record(self, request_id: str, checkin: dict, votes: list[dict], d: Decision,
                     escalated: bool = False, situation: str | None = None,
                     now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        traveller_id = checkin["travellerId"]

        # One report per problem type, strongest first. If one fails, the whole check-in is retried:
        # the ones already sent go again and only update their incident (same incident id).
        alerts = []
        if d.decision == "problem":
            for report in build_problem_reports(request_id, checkin, votes, d, now):
                incident = await self._report_problem(report)
                alerts.append({"problemType": report["problemType"], "incidentId": incident.get("incidentId"),
                               "created": incident.get("created")})

        before = Risk.from_hash(await self.redis.hgetall(risk_key(traveller_id)))
        after = update_risk(before, d, self.s.risk, failed=detectors_failed(votes))
        record = json.dumps(decision_record(request_id, checkin, votes, d, escalated, before, after, alerts, now,
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
        tx.set(decided_key(checkin["tripId"], int(checkin["seq"])), "1", ex=self.s.decided_ttl_s)
        await tx.execute()

        log.info("%s -> %s (k=%d, risk %s -> %s%s)", request_id, d.decision, len(votes), before.level,
                 after.level, "".join(f", incident {a['incidentId']}" for a in alerts))
        return summary(d, after.level)
