"""Weighted vote over detector votes, per problem type. Pure functions, no I/O.

The detectors are diverse: each one checks a different thing (detectors/*.py, Member 4):

    route-corridor   far from the planned path?            -> off_route
    heading-speed    moving against the route direction?   -> wrong_direction
    dwell-time       standing in one place too long?       -> long_dwell
    ml-anomaly       anything unusual (generalist)          -> any type

So "normal" from dwell-time means "not stuck", not "on the route". A plain majority
over all votes would let detectors that don't look at the route outvote the one that
does, and an off-route traveller would be judged normal: a missed alert.

Instead, every problem type that is raised is voted on by its own jury: the detectors
that check that type (COVERAGE), plus whoever raised it.

    weight        = vote confidence x detector reliability (detector:weights, default 1.0)
    problem score = weight saying "this problem" / weight of the jury's real votes

Abstains don't count: "I don't know" is evidence neither way. A problem vote of a
*different* type is neither for nor against this one.

For each raised type:
  - a jury of one, while another detector that checks this type hasn't been asked yet,
    or a close call (score within `close_margin` of the threshold) while one hasn't:
    pending -> the Coordinator asks the remaining detectors and resubmits
  - otherwise confirmed if score >= threshold (exactly at it counts as a problem:
    a missed alert is the unsafe failure), else rejected

Decision: "problem" if any type is confirmed (the strongest one is reported); else
"inconclusive" with needs_more_votes if any is pending; else "normal". Fewer real votes
in total than the quorum is "inconclusive" before any of this.
"""
from collections import defaultdict
from dataclasses import dataclass

COUNTED = {"problem", "normal"}
UNKNOWN = "unknown"             # a "problem" vote without a type
ANY = "*"
ALL_DETECTORS = ("route-corridor", "heading-speed", "dwell-time", "ml-anomaly")

# Which problem types each detector actually checks. Unlisted detectors are treated as
# generalists. A "problem" without a type is judged by everyone, like a plain majority.
COVERAGE: dict[str, frozenset] = {
    "route-corridor": frozenset({"off_route"}),
    "heading-speed": frozenset({"wrong_direction"}),
    "dwell-time": frozenset({"long_dwell"}),
    "ml-anomaly": frozenset({ANY}),
}


@dataclass(frozen=True)
class Decision:
    decision: str                  # "problem" | "normal" | "inconclusive"
    problem_type: str | None       # e.g. "off_route"; None unless decision is "problem"
    confidence: float              # how sure we are of `decision`, 0-1
    problem_score: float | None    # problem: the reported type's score; normal: the highest
                                   # rejected score (0 if nothing was raised); None if no weight
    counted: int                   # votes that took part (not abstains)
    reason: str | None = None      # why inconclusive: "quorum" | "no_weight" | "split" | "single_voter"
    needs_more_votes: bool = False # inconclusive and some detectors weren't asked yet


def _clamp(x, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return lo


def vote_weight(vote: dict, reliability: dict[str, float]) -> float:
    return _clamp(vote.get("confidence")) * max(0.0, float(reliability.get(vote.get("detectorId"), 1.0)))


def checks(detector: str | None, problem_type: str, coverage: dict[str, frozenset] = COVERAGE) -> bool:
    """Does this detector judge this kind of problem?"""
    if problem_type == UNKNOWN:
        return True
    covered = coverage.get(detector, frozenset({ANY}))
    return ANY in covered or problem_type in covered


def _type_of(vote: dict) -> str:
    return vote.get("problemType") or UNKNOWN


@dataclass(frozen=True)
class _TypeVerdict:
    problem_type: str
    state: str          # "confirmed" | "rejected" | "pending"
    score: float
    weight_for: float
    reason: str | None  # why pending


def _judge_type(problem_type: str, counted: list[dict], asked: set[str], reliability: dict[str, float],
                threshold: float, close_margin: float, all_detectors: tuple[str, ...],
                coverage: dict[str, frozenset]) -> _TypeVerdict | None:
    jury = [v for v in counted
            if (v["verdict"] == "problem" and _type_of(v) == problem_type)
            or (v["verdict"] == "normal" and checks(v.get("detectorId"), problem_type, coverage))]
    w_for = sum(vote_weight(v, reliability) for v in jury if v["verdict"] == "problem")
    w_total = sum(vote_weight(v, reliability) for v in jury)
    if w_total == 0:
        return None
    score = w_for / w_total
    unasked_judges = [d for d in all_detectors if d not in asked and checks(d, problem_type, coverage)]
    if unasked_judges and len(jury) < 2:
        return _TypeVerdict(problem_type, "pending", score, w_for, "single_voter")
    if unasked_judges and abs(score - threshold) <= close_margin:
        return _TypeVerdict(problem_type, "pending", score, w_for, "split")
    return _TypeVerdict(problem_type, "confirmed" if score >= threshold else "rejected", score, w_for, None)


def aggregate(votes: list[dict], reliability: dict[str, float] | None = None,
              threshold: float = 0.5, quorum: int = 2, close_margin: float = 0.1,
              all_detectors: tuple[str, ...] = ALL_DETECTORS,
              coverage: dict[str, frozenset] = COVERAGE) -> Decision:
    reliability = reliability or {}
    asked = {v.get("detectorId") for v in votes}
    can_ask_more = any(d not in asked for d in all_detectors)
    counted = [v for v in votes if v.get("verdict") in COUNTED]

    def inconclusive(reason: str, score: float | None = None, more: bool = can_ask_more) -> Decision:
        return Decision("inconclusive", None, 0.0, score, len(counted), reason, more)

    if len(counted) < quorum:
        return inconclusive("quorum")
    if sum(vote_weight(v, reliability) for v in counted) == 0:
        # Votes were cast but all with zero confidence or zero reliability.
        return inconclusive("no_weight")

    raised = sorted({_type_of(v) for v in counted if v["verdict"] == "problem"})
    verdicts = [t for t in (_judge_type(p, counted, asked, reliability, threshold, close_margin,
                                        all_detectors, coverage) for p in raised) if t is not None]

    confirmed = [t for t in verdicts if t.state == "confirmed"]
    if confirmed:
        # The strongest confirmed problem: highest score, then most weight, then alphabetical.
        best = min(confirmed, key=lambda t: (-t.score, -t.weight_for, t.problem_type))
        reported = None if best.problem_type == UNKNOWN else best.problem_type
        return Decision("problem", reported, best.score, best.score, len(counted))

    pending = [t for t in verdicts if t.state == "pending"]
    if pending:
        reason = "split" if any(t.reason == "split" for t in pending) else "single_voter"
        return inconclusive(reason, max(t.score for t in pending), more=True)

    top = max((t.score for t in verdicts), default=0.0)
    return Decision("normal", None, 1 - top, top, len(counted))
