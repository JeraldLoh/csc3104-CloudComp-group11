"""Rule-based decision over the detectors' votes. A missed alert is the unsafe failure, so:

  1. Any detector that checks a type (COVERAGE) says "problem" of that type -> problem.
     A "normal" never cancels it: dwell-time's "normal" means "not stuck", not "on route".
  2. Otherwise "normal" needs at least `quorum` real votes; fewer -> inconclusive.

weight = vote confidence x detector reliability (detector:weights, default 1.0). A silenced
detector (reliability 0) is ignored completely. Every type raised is reported, strongest first.
"""
from dataclasses import dataclass

from .detectors import ALL_DETECTORS

COUNTED = {"problem", "normal"}
UNKNOWN = "unknown"             # a "problem" vote without a type
ANY = "*"

# Which problem types each detector checks (contracts/problem-definitions.md).
COVERAGE: dict[str, frozenset] = {
    "route-corridor": frozenset({"off_route"}),
    "heading-speed": frozenset({"wrong_direction"}),
    "dwell-time": frozenset({"long_dwell"}),
    "route-progress": frozenset({"off_route", "wrong_direction", "missed_stop"}),
}


@dataclass(frozen=True)
class Decision:
    decision: str                  # "problem" | "normal" | "inconclusive"
    problem_type: str | None       # e.g. "off_route"; None unless decision is "problem"
    confidence: float              # how sure we are of `decision`, 0-1
    problem_score: float | None    # weight of the strongest accepted problem claim (0 if none);
                                   # None if inconclusive
    counted: int                   # votes that took part (not abstains)
    reason: str | None = None      # why inconclusive: "quorum" | "no_weight"
    needs_more_votes: bool = False # inconclusive and some detectors weren't asked yet
    claims: tuple = ()             # every problem type raised, strongest first: ((type, weight), ...)
    silenced: tuple = ()           # detectors that answered but were ignored (reliability 0), sorted


def _clamp(x, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return lo


def vote_weight(vote: dict, reliability: dict[str, float]) -> float:
    return _clamp(vote.get("confidence")) * max(0.0, float(reliability.get(vote.get("detectorId"), 1.0)))


def silenced(detector: str | None, reliability: dict[str, float]) -> bool:
    """Reliability 0 (detector:weights): an operator has switched this detector off."""
    try:
        return float(reliability.get(detector, 1.0)) <= 0
    except (TypeError, ValueError):
        return False


def checks(detector: str | None, problem_type: str, coverage: dict[str, frozenset] = COVERAGE) -> bool:
    """Does this detector judge this kind of problem?"""
    if problem_type == UNKNOWN:
        return True
    covered = coverage.get(detector, frozenset({ANY}))
    return ANY in covered or problem_type in covered


def _type_of(vote: dict) -> str:
    return vote.get("problemType") or UNKNOWN


def aggregate(votes: list[dict], reliability: dict[str, float] | None = None, quorum: int = 2,
              all_detectors: tuple[str, ...] = ALL_DETECTORS,
              coverage: dict[str, frozenset] = COVERAGE) -> Decision:
    reliability = reliability or {}
    asked = {v.get("detectorId") for v in votes}
    can_ask_more = any(d not in asked for d in all_detectors)
    # Silenced detectors are left out here, so they count for neither rule below.
    off = tuple(sorted({v.get("detectorId") for v in votes if silenced(v.get("detectorId"), reliability)}))
    counted = [v for v in votes
               if v.get("verdict") in COUNTED and not silenced(v.get("detectorId"), reliability)]

    # Rule 1: one credible problem claim is enough.
    claims = [(vote_weight(v, reliability), _type_of(v)) for v in counted
              if v["verdict"] == "problem" and checks(v.get("detectorId"), _type_of(v), coverage)]
    claims = [(w, t) for w, t in claims if w > 0]
    if claims:
        # One entry per type at its strongest claim; strongest first, equal weights alphabetically.
        strongest: dict[str, float] = {}
        for weight, problem_type in claims:
            strongest[problem_type] = max(weight, strongest.get(problem_type, 0.0))
        ranked = sorted(strongest.items(), key=lambda c: (-c[1], c[0]))
        reported = tuple((None if t == UNKNOWN else t, w) for t, w in ranked)
        problem_type, weight = reported[0]
        return Decision("problem", problem_type, weight, weight, len(counted), claims=reported, silenced=off)

    # Rule 2: "normal" needs enough detectors to have actually looked.
    if len(counted) < quorum:
        return Decision("inconclusive", None, 0.0, None, len(counted), "quorum", can_ask_more, silenced=off)
    if sum(vote_weight(v, reliability) for v in counted) == 0:
        return Decision("inconclusive", None, 0.0, None, len(counted), "no_weight", can_ask_more, silenced=off)
    return Decision("normal", None, 1.0, 0.0, len(counted), silenced=off)
