"""Per-traveller risk score: decides whether the Coordinator asks 2 or 4 detectors next.
Pure functions, no I/O. Stored in Redis as risk:{travellerId} (contracts/redis-keys.md).

After every final decision the score moves towards a signal (exponential moving average):

    score  = (1 - alpha) * score + alpha * signal
    signal = 1.0                      decision "problem"
             problem score (0..1)     decision "normal": a shaky normal nudges risk up
             1.0                      still inconclusive after every detector: we're blind

The level has hysteresis, so k doesn't flip-flop between 2 and 4:
  - normal -> elevated as soon as score >= escalate_at (one problem is enough)
  - elevated -> normal only when score < deescalate_below AND the last `calm_checkins`
    decisions were all "normal"
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
    return 1.0   # final inconclusive: no usable evidence, so keep looking hard


def update_risk(prev: Risk, d: Decision, policy: RiskPolicy = RiskPolicy()) -> Risk:
    if d.needs_more_votes:
        return prev   # not a final decision; the Coordinator is still collecting votes

    score = min(1.0, max(0.0, (1 - policy.alpha) * prev.score + policy.alpha * signal(d)))
    streak = prev.calm_streak + 1 if d.decision == "normal" else 0

    level = prev.level
    if level == NORMAL and score >= policy.escalate_at:
        level = ELEVATED
    elif level == ELEVATED and score < policy.deescalate_below and streak >= policy.calm_checkins:
        level = NORMAL
    return Risk(score, level, streak)
