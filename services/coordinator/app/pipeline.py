"""What the Coordinator does with one check-in.

    1. Already decided (tripId + seq)?     SQS delivers at least once: skip repeats
    2. Read risk:{travellerId}             normal / elevated
    3. Ask detectors and decide            REDUNDANCY_MODE picks how:
         fixed4 (default)    _ask_all         all 4 in parallel, decide once (rule-based,
                                              decision.py, voting.py). Too few real votes ->
                                              inconclusive: there is no one left to ask.
         adaptive / fixed2   _ask_pair_first  the experiment: a pair that fits the journey step
                                              first (adaptive: all 4 when risk is elevated); too
                                              few real votes or still undecided -> ask the rest
       A detector that times out or fails counts as an abstain (detectors.py).
    4. Record                              alert, risk, dashboard record, mark decided

Redis or Alert Service failures raise. The caller (the SQS consumer) then doesn't delete the
message, so it comes back after the visibility timeout and any replica retries it.
Step 3 changes nothing, and step 4 only writes after the alert went out, in one transaction
(decision.py), so a retry costs at most a repeated report, never a lost check-in.
"""
import logging
from dataclasses import dataclass

from .config import Settings
from .decision import DecisionRecorder, decided_key
from .detectors import DetectorClient
from .selection import ALL_DETECTORS, STRATEGIES, choose_detectors, needs_escalation, pair_for_step, \
    remaining, step_mode
from .voting import Decision

log = logging.getLogger("coordinator")


@dataclass
class Asked:
    """What asking the detectors produced: their votes and the decision on them."""
    votes: list[dict]
    d: Decision
    escalated: bool = False      # asked more than the first choice
    situation: str | None = None # why that pair (None = all asked)


@dataclass
class Result:
    outcome: str                 # "decided" or "duplicate"
    k: int = 0                   # detectors asked
    escalated: bool = False      # asked more than the first choice
    decision: dict | None = None # decision.summary(): decision, problemType, confidence, reason, riskLevel
    situation: str | None = None # why that pair: "walking", "waiting", "riding", ... (None = all asked)


class Pipeline:
    def __init__(self, redis, detectors: DetectorClient, decisions: DecisionRecorder, s: Settings):
        self.redis = redis
        self.detectors = detectors
        self.decisions = decisions
        self.s = s

    async def _ask_all(self, request: dict) -> Asked:
        """fixed4 (the default): every detector, one round."""
        votes = await self.detectors.call_many(list(ALL_DETECTORS), request)
        return Asked(votes, await self.decisions.decide(votes))

    async def _ask_pair_first(self, checkin: dict, risk_level: str, request: dict) -> Asked:
        """adaptive / fixed2 (the experiment): a pair first, then the rest if the pair can't decide."""
        request_id = request["requestId"]
        asked = choose_detectors(risk_level, self.s.redundancy_mode, self.s.normal_pair)
        situation = None
        if len(asked) == 2:   # a pair, not everyone: pick the one that fits the journey step
            pair, situation = await self._pair_for(checkin)
            asked = choose_detectors(risk_level, self.s.redundancy_mode, pair)
        votes = await self.detectors.call_many(asked, request)
        escalated = False

        # Too few real votes: no point deciding yet.
        if needs_escalation(votes, self.s.quorum):
            extra = remaining(asked)
            log.info("escalating %s: too few real votes, asking %s", request_id, extra)
            votes += await self.detectors.call_many(extra, request)
            asked += extra
            escalated = True

        d = await self.decisions.decide(votes)

        # Still can't call it (e.g. the answers carry no weight): more evidence instead of a guess.
        extra = remaining(asked)
        if d.needs_more_votes and extra:
            log.info("escalating %s: %s, asking %s", request_id, d.reason, extra)
            votes += await self.detectors.call_many(extra, request)
            escalated = True
            d = await self.decisions.decide(votes)
        return Asked(votes, d, escalated, situation)

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
        request = {"requestId": request_id, "riskLevel": risk_level, "checkin": checkin}
        if self.s.redundancy_mode == "fixed4":
            a = await self._ask_all(request)
        else:
            a = await self._ask_pair_first(checkin, risk_level, request)

        decision = await self.decisions.record(request_id, checkin, a.votes, a.d, a.escalated, a.situation)
        # k is logged for every decision: the experiment and the dashboard depend on it.
        log.info("decided %s k=%d escalated=%s risk=%s step=%s -> %s", request_id, len(a.votes), a.escalated,
                 risk_level, a.situation or "-", decision["decision"])
        return Result(outcome="decided", k=len(a.votes), escalated=a.escalated, decision=decision,
                      situation=a.situation)
