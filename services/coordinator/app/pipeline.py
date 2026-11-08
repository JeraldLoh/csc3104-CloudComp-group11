"""What the Coordinator does with one check-in.

    1. Already decided (tripId + seq)?     SQS delivers at least once: skip repeats
    2. Read risk:{travellerId}             normal / elevated
    3. Ask all 4 detectors, decide         in parallel, decide once (rule-based, decision.py,
                                           voting.py). Too few real votes -> inconclusive:
                                           there is no one left to ask.
       A detector that times out or fails counts as an abstain (detectors.py).
    4. Record                              alert, risk, dashboard record, mark decided

That is fixed4, the default (REDUNDANCY_MODE). The experiment's other arms (adaptive, fixed2:
ask a pair first) are in adaptive.py; only step 3 differs.

Redis or Alert Service failures raise. The caller (the SQS consumer) then doesn't delete the
message, so it comes back after the visibility timeout and any replica retries it.
Step 3 changes nothing, and step 4 only writes after the alert went out, in one transaction
(decision.py), so a retry costs at most a repeated report, never a lost check-in.
"""
import logging
from dataclasses import dataclass

from . import adaptive
from .config import Settings
from .decision import DecisionRecorder, decided_key
from .detectors import ALL_DETECTORS, DetectorClient

log = logging.getLogger("coordinator")


@dataclass
class Result:
    outcome: str                 # "decided" or "duplicate"
    k: int = 0                   # detectors asked
    escalated: bool = False      # asked more than the first choice (adaptive / fixed2)
    decision: dict | None = None # decision.summary(): decision, problemType, confidence, reason, riskLevel
    situation: str | None = None # why that pair: "walking", "waiting", "riding", ... (None = all asked)


class Pipeline:
    def __init__(self, redis, detectors: DetectorClient, decisions: DecisionRecorder, s: Settings):
        self.redis = redis
        self.detectors = detectors
        self.decisions = decisions
        self.s = s

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
        request = {"requestId": request_id, "riskLevel": risk_level, "checkin": checkin}
        if self.s.redundancy_mode == "fixed4":
            votes = await self.detectors.call_many(list(ALL_DETECTORS), request)
            d = await self.decisions.decide(votes)
            escalated, situation = False, None
        else:   # the experiment (evaluation C5)
            votes, d, escalated, situation = await adaptive.ask_pair_first(
                self.detectors, self.decisions, self.redis, self.s, checkin, risk_level, request)

        decision = await self.decisions.record(request_id, checkin, votes, d, escalated, situation)
        # k is logged for every decision: the experiment and the dashboard depend on it.
        log.info("decided %s k=%d escalated=%s risk=%s step=%s -> %s", request_id, len(votes), escalated,
                 risk_level, situation or "-", decision["decision"])
        return Result(outcome="decided", k=len(votes), escalated=escalated, decision=decision,
                      situation=situation)
