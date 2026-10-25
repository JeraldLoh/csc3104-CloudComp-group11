"""Settings for the Aggregator, read from environment variables."""
import os
from dataclasses import dataclass, field

from .risk import RiskPolicy


def _float(name: str, default: str) -> float:
    return float(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    # contracts/api.md: http://alert-service:8000 inside the cluster.
    alert_url: str = field(default_factory=lambda: os.getenv("ALERT_SERVICE_URL", "http://alert-service:8000").rstrip("/"))
    alert_timeout_s: float = field(default_factory=lambda: _float("ALERT_TIMEOUT_S", "3"))

    # Voting (voting.py)
    threshold: float = field(default_factory=lambda: _float("VOTE_THRESHOLD", "0.5"))
    quorum: int = field(default_factory=lambda: int(os.getenv("QUORUM", "2")))
    close_margin: float = field(default_factory=lambda: _float("CLOSE_MARGIN", "0.1"))

    # Risk (risk.py)
    risk: RiskPolicy = field(default_factory=lambda: RiskPolicy(
        alpha=_float("RISK_ALPHA", "0.5"),
        escalate_at=_float("RISK_ESCALATE_AT", "0.4"),
        deescalate_below=_float("RISK_DEESCALATE_BELOW", "0.15"),
        calm_checkins=int(os.getenv("RISK_CALM_CHECKINS", "3")),
    ))

    # Journey keys expire 24 h after their last update (contracts/redis-keys.md).
    key_ttl_s: int = field(default_factory=lambda: int(os.getenv("KEY_TTL_S", "86400")))
    # Decision records kept for the dashboard.
    decisions_per_traveller: int = field(default_factory=lambda: int(os.getenv("DECISIONS_PER_TRAVELLER", "50")))
    decisions_recent: int = field(default_factory=lambda: int(os.getenv("DECISIONS_RECENT", "200")))


settings = Settings()
