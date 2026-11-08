"""What the Coordinator does with one check-in.

    1. Already decided (tripId + seq)?     SQS delivers at least once: skip repeats
    2. Read risk:{travellerId}             normal / elevated (written by the Aggregator)
    3. Choose detectors                    k = 2 or 4; at k = 2 the pair fits the journey
                                           step, from trip:{tripId}:route (selection.py)
    4. Call them in parallel               timeout or error = abstain (detectors.py)
    5. Too few real votes?                 ask the remaining detectors straight away
    6. POST /internal/votes (Aggregator)   it decides, alerts and updates risk
    7. Aggregator says needsMoreVotes?     a close call: ask the remaining detectors, resubmit
    8. Remember tripId + seq as decided

Redis or Aggregator failures raise. The caller (the SQS consumer) then doesn't delete the
message, so it comes back after the visibility timeout and any replica retries it.
Steps 4-7 are safe to repeat (the Alert Service deduplicates incidents), so a retry costs
at most a repeated report, never a lost check-in.
"""
import logging
from dataclasses import dataclass

import httpx

from .config import Settings
from .detectors import DetectorClient
from .selection import STRATEGIES, choose_detectors, needs_escalation, pair_for_step, remaining, step_mode

log = logging.getLogger("coordinator")


@dataclass
class Result:
    outcome: str                 # "decided" or "duplicate"
    k: int = 0                   # detectors asked
    escalated: bool = False      # asked more than the first choice
    decision: dict | None = None # the Aggregator's answer
    situation: str | None = None # why that pair: "walking", "waiting", "riding", ... (None = all asked)


def decided_key(trip_id: str, seq: int) -> str:
    return f"decided:{trip_id}:{seq}"


class Pipeline:
    def __init__(self, redis, http: httpx.AsyncClient, detectors: DetectorClient, s: Settings):
        self.redis = redis
        self.http = http
        self.detectors = detectors
        self.s = s

    async def _aggregate(self, request_id: str, checkin: dict, votes: list[dict],
                         risk_level: str, escalated: bool, situation: str | None = None) -> dict:
        resp = await self.http.post(
            f"{self.s.aggregator_url}/internal/votes",
            json={"requestId": request_id, "checkin": checkin, "votes": votes,
                  "riskLevel": risk_level, "escalated": escalated, "situation": situation},
            timeout=self.s.aggregator_timeout_s,
        )
        resp.raise_for_status()
        return resp.json()

    async def _pair_for(self, checkin: dict) -> tuple[tuple[str, ...], str]:
        """The pair to ask at normal risk, and why (PAIR_STRATEGY)."""
        if self.s.pair_strategy not in STRATEGIES:
            raise ValueError(f"unknown PAIR_STRATEGY {self.s.pair_strategy!r}; expected one of {STRATEGIES}")
        if self.s.pair_strategy == "fixed":
            return self.s.normal_pair, "fixed"
        # The step comes with the check-in; its mode from the trip's route (Journey Service).
        route = await self.redis.get(f"trip:{checkin['tripId']}:route")
        mode = step_mode(route, checkin.get("stepIndex"))
        return pair_for_step(mode, checkin.get("speedMps"), self.s.waiting_below_mps)

    async def process(self, checkin: dict) -> Result:
        try:
            trip_id, seq, traveller_id = checkin["tripId"], int(checkin["seq"]), checkin["travellerId"]
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"invalid check-in, missing or bad {e}") from e
        request_id = f"{trip_id}#{seq}"

        if await self.redis.exists(decided_key(trip_id, seq)):
            log.info("skip %s: already decided", request_id)
            return Result(outcome="duplicate")

        risk_level = await self.redis.hget(f"risk:{traveller_id}", "level") or "normal"
        asked = choose_detectors(risk_level, self.s.redundancy_mode, self.s.normal_pair)
        situation = None
        if len(asked) == 2:   # a pair, not everyone: pick the one that fits the journey step
            pair, situation = await self._pair_for(checkin)
            asked = choose_detectors(risk_level, self.s.redundancy_mode, pair)
        request = {"requestId": request_id, "riskLevel": risk_level, "checkin": checkin}
        votes = await self.detectors.call_many(asked, request)
        escalated = False

        # Too few real votes: no point asking the Aggregator yet.
        if needs_escalation(votes, self.s.quorum):
            extra = remaining(asked)
            log.info("escalating %s: too few real votes, asking %s", request_id, extra)
            votes += await self.detectors.call_many(extra, request)
            asked += extra
            escalated = True

        decision = await self._aggregate(request_id, checkin, votes, risk_level, escalated, situation)

        # A close call: more evidence instead of a coin flip.
        extra = remaining(asked)
        if decision.get("needsMoreVotes") and extra:
            log.info("escalating %s: %s, asking %s", request_id, decision.get("reason"), extra)
            votes += await self.detectors.call_many(extra, request)
            asked += extra
            escalated = True
            decision = await self._aggregate(request_id, checkin, votes, risk_level, escalated, situation)

        await self.redis.set(decided_key(trip_id, seq), "1", ex=self.s.decided_ttl_s)
        # k is logged for every decision: the experiment and the dashboard depend on it.
        log.info("decided %s k=%d escalated=%s risk=%s step=%s -> %s", request_id, len(votes), escalated,
                 risk_level, situation or "-", decision.get("decision"))
        return Result(outcome="decided", k=len(votes), escalated=escalated, decision=decision,
                      situation=situation)
