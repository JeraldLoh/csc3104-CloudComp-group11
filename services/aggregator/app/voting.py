"""Weighted majority vote over detector votes. Pure functions, no I/O.

    weight        = vote confidence x detector reliability (detector:weights, default 1.0)
    problem score = weight voting "problem" / weight of all counted votes

Abstains don't count: "I don't know" is evidence neither way.

When the evidence is too thin, the result is "inconclusive" instead of a guess:
  - fewer counted votes than the quorum, or
  - a close call: the score is within `close_margin` of the threshold (e.g. 1 vs 1).
If not every detector was asked yet, `needs_more_votes` is set and the Coordinator asks
the remaining ones and resubmits: disagreement buys more evidence, not a coin flip.

Once every detector has voted there is nothing left to ask, so a close call is decided
by the threshold, and a score exactly at it counts as a problem: a missed alert is the
unsafe failure.
"""
from collections import defaultdict
from dataclasses import dataclass

COUNTED = {"problem", "normal"}
ALL_DETECTORS = 4


@dataclass(frozen=True)
class Decision:
    decision: str                  # "problem" | "normal" | "inconclusive"
    problem_type: str | None       # e.g. "off_route"; None unless decision is "problem"
    confidence: float              # how sure we are of `decision`, 0-1
    problem_score: float | None    # share of weight voting "problem"; None if no weight
    counted: int                   # votes that took part (not abstains)
    reason: str | None = None      # why inconclusive: "quorum" | "no_weight" | "split"
    needs_more_votes: bool = False # inconclusive and some detectors weren't asked yet


def _clamp(x, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return lo


def vote_weight(vote: dict, reliability: dict[str, float]) -> float:
    return _clamp(vote.get("confidence")) * max(0.0, float(reliability.get(vote.get("detectorId"), 1.0)))


def aggregate(votes: list[dict], reliability: dict[str, float] | None = None,
              threshold: float = 0.5, quorum: int = 2, close_margin: float = 0.1,
              all_detectors: int = ALL_DETECTORS) -> Decision:
    reliability = reliability or {}
    can_ask_more = len(votes) < all_detectors
    counted = [v for v in votes if v.get("verdict") in COUNTED]

    def inconclusive(reason: str, score: float | None = None) -> Decision:
        return Decision("inconclusive", None, 0.0, score, len(counted), reason, can_ask_more)

    if len(counted) < quorum:
        return inconclusive("quorum")

    problem_w = sum(vote_weight(v, reliability) for v in counted if v["verdict"] == "problem")
    normal_w = sum(vote_weight(v, reliability) for v in counted if v["verdict"] == "normal")
    total = problem_w + normal_w
    if total == 0:
        # Votes were cast but all with zero confidence or zero reliability.
        return inconclusive("no_weight")

    score = problem_w / total
    if can_ask_more and abs(score - threshold) <= close_margin:
        return inconclusive("split", score)
    if score >= threshold:
        return Decision("problem", _problem_type(counted, reliability), score, score, len(counted))
    return Decision("normal", None, 1 - score, score, len(counted))


def _problem_type(votes: list[dict], reliability: dict[str, float]) -> str | None:
    """The problem type with the most weight behind it (ties: alphabetical, so it's stable)."""
    by_type: dict[str, float] = defaultdict(float)
    for v in votes:
        if v["verdict"] == "problem" and v.get("problemType"):
            by_type[v["problemType"]] += vote_weight(v, reliability)
    if not by_type:
        return None
    return min(by_type, key=lambda t: (-by_type[t], t))
