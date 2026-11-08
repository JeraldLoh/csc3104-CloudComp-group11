"""The experiment (evaluation C5): asking fewer detectors. Not used in fixed4, the default.

REDUNDANCY_MODE picks the arm; pipeline.py calls ask_pair_first() for any mode but fixed4:

    adaptive   k = 2 when the traveller's risk is normal, k = 4 (every detector) when elevated
    fixed2     k = 2 always

Either way, too few real votes, or still undecided, asks the remaining detectors too.

Which pair is asked at k = 2 depends on PAIR_STRATEGY:

    step   (default) route-corridor plus the detector that fits the current journey step:
               walking                      + heading-speed   (wrong direction)
               bus/train step, slow         + dwell-time      (waiting too long for it)
               bus/train step, moving       + route-progress  (missed stop)
               step unknown                 + route-progress  (covers the most problem types)
           A bus/train step covers both waiting at the stop and riding, so the check-in's
           speed tells them apart.
    fixed  the same pair for every check-in (NORMAL_PAIR, default route-corridor + dwell-time).

The pair choice and escalation rules are pure functions; ask_pair_first() does the calls.
"""
import json
import logging

from .config import DEFAULT_PAIR, Settings
from .detectors import ALL_DETECTORS, DetectorClient
from .voting import Decision

log = logging.getLogger("coordinator")

MODES = ("adaptive", "fixed2", "fixed4")
STRATEGIES = ("step", "fixed")

# Step modes in contracts/examples/route.json; "train" as the detectors use it, "mrt" just in case.
TRANSIT_MODES = frozenset({"bus", "train", "mrt"})
WALKING_PAIR = ("route-corridor", "heading-speed")
WAITING_PAIR = ("route-corridor", "dwell-time")
RIDING_PAIR = ("route-corridor", "route-progress")
UNKNOWN_STEP_PAIR = ("route-corridor", "route-progress")


def step_mode(route_json: str | None, step_index) -> str | None:
    """Mode ("walk", "bus", ...) of step `step_index` in trip:{tripId}:route, or None if unknown."""
    if not route_json or step_index is None:
        return None
    try:
        steps = json.loads(route_json)["steps"]
        i = int(step_index)
    except (ValueError, TypeError, KeyError):
        return None
    if not isinstance(steps, list) or not 0 <= i < len(steps) or not isinstance(steps[i], dict):
        return None
    mode = steps[i].get("mode")
    return mode.lower() if isinstance(mode, str) else None


def pair_for_step(mode: str | None, speed_mps, waiting_below_mps: float = 2.0) -> tuple[tuple[str, ...], str]:
    """(pair, situation) for the journey step. situation is for logs and the dashboard."""
    if mode == "walk":
        return WALKING_PAIR, "walking"
    if mode in TRANSIT_MODES:
        try:
            moving = speed_mps is not None and float(speed_mps) >= waiting_below_mps
        except (TypeError, ValueError):
            moving = False
        return (RIDING_PAIR, "riding") if moving else (WAITING_PAIR, "waiting")
    return UNKNOWN_STEP_PAIR, "unknown step"


def choose_detectors(risk_level: str, mode: str = "adaptive",
                     pair: tuple[str, ...] = DEFAULT_PAIR) -> list[str]:
    """Detectors to call first. `mode` is REDUNDANCY_MODE (the experiment's three arms)."""
    if mode not in MODES:
        raise ValueError(f"unknown REDUNDANCY_MODE {mode!r}; expected one of {MODES}")
    if mode == "fixed4" or (mode == "adaptive" and risk_level == "elevated"):
        return list(ALL_DETECTORS)
    return list(pair)


def counted_votes(votes: list[dict]) -> int:
    """Votes that take part in the decision (abstains don't)."""
    return sum(1 for v in votes if v.get("verdict") != "abstain")


def needs_escalation(votes: list[dict], quorum: int) -> bool:
    """Too few real votes to trust the result: ask the remaining detectors too."""
    return counted_votes(votes) < quorum and len(votes) < len(ALL_DETECTORS)


def remaining(already_called: list[str]) -> list[str]:
    return [d for d in ALL_DETECTORS if d not in already_called]


async def pair_for(redis, s: Settings, checkin: dict) -> tuple[tuple[str, ...], str]:
    """The pair to ask at normal risk, and why (PAIR_STRATEGY)."""
    if s.pair_strategy not in STRATEGIES:
        raise ValueError(f"unknown PAIR_STRATEGY {s.pair_strategy!r}; expected one of {STRATEGIES}")
    if s.pair_strategy == "fixed":
        return s.normal_pair, "fixed"
    # The step comes with the check-in; its mode from the trip's route (Journey Service).
    route = await redis.get(f"trip:{checkin['tripId']}:route")
    mode = step_mode(route, checkin.get("stepIndex"))
    return pair_for_step(mode, checkin.get("speedMps"), s.waiting_below_mps)


async def ask_pair_first(detectors: DetectorClient, decisions, redis, s: Settings, checkin: dict,
                         risk_level: str, request: dict) -> tuple[list[dict], Decision, bool, str | None]:
    """adaptive / fixed2: a pair first, then the rest if the pair can't decide.
    Returns (votes, decision, escalated, situation); situation is why that pair (None = all asked)."""
    request_id = request["requestId"]
    asked = choose_detectors(risk_level, s.redundancy_mode, s.normal_pair)
    situation = None
    if len(asked) == 2:   # a pair, not everyone: pick the one that fits the journey step
        pair, situation = await pair_for(redis, s, checkin)
        asked = choose_detectors(risk_level, s.redundancy_mode, pair)
    votes = await detectors.call_many(asked, request)
    escalated = False

    # Too few real votes: no point deciding yet.
    if needs_escalation(votes, s.quorum):
        extra = remaining(asked)
        log.info("escalating %s: too few real votes, asking %s", request_id, extra)
        votes += await detectors.call_many(extra, request)
        asked += extra
        escalated = True

    d = await decisions.decide(votes)

    # Still can't call it (e.g. the answers carry no weight): more evidence instead of a guess.
    extra = remaining(asked)
    if d.needs_more_votes and extra:
        log.info("escalating %s: %s, asking %s", request_id, d.reason, extra)
        votes += await detectors.call_many(extra, request)
        escalated = True
        d = await decisions.decide(votes)
    return votes, d, escalated, situation
