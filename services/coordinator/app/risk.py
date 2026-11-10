"""Per-traveller risk score (risk:{travellerId}), shown on the dashboard. In adaptive mode it
also decides whether 2 or 4 detectors are asked.

After each final decision: score = (1 - alpha) * score + alpha * signal, where signal is 1.0
for a problem, the problem score for a normal, and 1.0 for an inconclusive caused by failed
detectors. An inconclusive where the detectors couldn't judge yet leaves the risk unchanged.

normal -> elevated once score >= escalate_at; back to normal only below deescalate_below and
after `calm_checkins` normal decisions in a row.
"""
from dataclasses import dataclass

from .voting import Decision

NORMAL, ELEVATED = "normal", "elevated"


@dataclass(frozen=True)
class RiskPolicy:
    alpha: float = 0.5
    escalate_at: float = 0.4
    deescalate_below: float = 0.15
    calm_checkins: int = 3

    def __post_init__(self):
        if not 0 < self.alpha <= 1:
            raise ValueError("alpha must be in (0, 1]")
        if not 0 <= self.deescalate_below < self.escalate_at <= 1:
            raise ValueError("need 0 <= deescalate_below < escalate_at <= 1")
        if self.calm_checkins < 0:
            raise ValueError("calm_checkins must be >= 0")


@dataclass(frozen=True)
class Risk:
    score: float = 0.0
    level: str = NORMAL
    calm_streak: int = 0      # consecutive "normal" decisions

    def to_hash(self) -> dict[str, str]:
        """Fields for the risk:{travellerId} hash (detectorsUsed/updatedAt are added by the caller)."""
        return {"score": f"{self.score:.4f}", "level": self.level, "calmStreak": str(self.calm_streak)}

    @classmethod
    def from_hash(cls, h: dict | None) -> "Risk":
        """Tolerant of a missing key (new traveller) or bad values: falls back to normal / 0."""
        h = h or {}
        try:
            score = min(1.0, max(0.0, float(h.get("score", 0))))
        except (TypeError, ValueError):
            score = 0.0
        level = ELEVATED if h.get("level") == ELEVATED else NORMAL
        try:
            streak = max(0, int(h.get("calmStreak", 0)))
        except (TypeError, ValueError):
            streak = 0
        return cls(score, level, streak)


def signal(d: Decision) -> float:
    if d.decision == "problem":
        return 1.0
    if d.decision == "normal":
        return d.problem_score or 0.0
    return 1.0   # inconclusive because detectors failed


# Abstain reasons that mean a detector failed (contracts/api.md), not "can't judge yet".
FAILURE_REASONS = ("timeout", "error:", "internal error")


def failed(vote: dict) -> bool:
    """An abstain that came from a failure (timeout, error), not from "can't judge yet"."""
    return vote.get("verdict") == "abstain" and str(vote.get("reason") or "").startswith(FAILURE_REASONS)


def detectors_failed(votes: list[dict]) -> bool:
    """True if any detector failed for this decision."""
    return any(failed(v) for v in votes)


def update_risk(prev: Risk, d: Decision, policy: RiskPolicy = RiskPolicy(), failed: bool = True) -> Risk:
    """`failed`: whether any detector failed for this decision (detectors_failed)."""
    if d.needs_more_votes:
        return prev   # not a final decision; the Coordinator is still collecting votes
    if d.decision == "inconclusive" and not failed:
        return prev   # detectors couldn't judge yet (e.g. start of a trip)

    score = min(1.0, max(0.0, (1 - policy.alpha) * prev.score + policy.alpha * signal(d)))
    streak = prev.calm_streak + 1 if d.decision == "normal" else 0

    level = prev.level
    if level == NORMAL and score >= policy.escalate_at:
        level = ELEVATED
    elif level == ELEVATED and score < policy.deescalate_below and streak >= policy.calm_checkins:
        level = NORMAL
    return Risk(score, level, streak)
