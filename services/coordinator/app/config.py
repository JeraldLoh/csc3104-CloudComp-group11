"""Settings for the Coordinator, read from environment variables."""
import os
from dataclasses import dataclass, field

from .detectors import parse_urls
from .risk import RiskPolicy

# The experiment's pair at normal risk with PAIR_STRATEGY=fixed (adaptive.py).
DEFAULT_PAIR = ("route-corridor", "dwell-time")


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: str) -> float:
    return float(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    # Unset in real AWS; set to a local SQS (ElasticMQ / LocalStack) for development.
    aws_endpoint_url: str | None = field(default_factory=lambda: os.getenv("AWS_ENDPOINT_URL") or None)
    aws_region: str = field(default_factory=lambda: os.getenv("AWS_DEFAULT_REGION", "ap-southeast-1"))
    # Either the full URL (deploy.sh fills __CHECKINS_QUEUE_URL__) or just the name.
    queue_url: str | None = field(default_factory=lambda: os.getenv("CHECKINS_QUEUE_URL") or None)
    queue_name: str = field(default_factory=lambda: os.getenv("CHECKINS_QUEUE", "careroute-checkins"))
    # Local development only: create the queue on startup.
    create_queue: bool = field(default_factory=lambda: _bool("CREATE_QUEUE", False))

    # fixed4 by default: every check-in is checked by every detector, because a missed alert is the
    # unsafe failure. adaptive and fixed2 are the experiment's other arms (see the end of this class).
    redundancy_mode: str = field(default_factory=lambda: os.getenv("REDUNDANCY_MODE", "fixed4"))
    detector_urls: dict = field(default_factory=lambda: parse_urls(os.getenv("DETECTOR_URLS", "")))
    # contracts/api.md: detectors must answer within 500 ms.
    detector_timeout_s: float = field(default_factory=lambda: float(os.getenv("DETECTOR_TIMEOUT_MS", "500")) / 1000)
    # Real (non-abstain) votes needed to call a check-in normal (one is enough for a problem);
    # fewer -> inconclusive (adaptive / fixed2: ask the remaining detectors too).
    quorum: int = field(default_factory=lambda: int(os.getenv("QUORUM", "2")))

    # contracts/api.md: http://alert-service:8000 inside the cluster.
    alert_url: str = field(default_factory=lambda: os.getenv("ALERT_SERVICE_URL", "http://alert-service:8000").rstrip("/"))
    alert_timeout_s: float = field(default_factory=lambda: _float("ALERT_TIMEOUT_S", "3"))

    # Risk (risk.py): shown on the dashboard; in adaptive mode it also picks 2 or 4 detectors.
    risk: RiskPolicy = field(default_factory=lambda: RiskPolicy(
        alpha=_float("RISK_ALPHA", "0.5"),
        escalate_at=_float("RISK_ESCALATE_AT", "0.4"),
        deescalate_below=_float("RISK_DEESCALATE_BELOW", "0.15"),
        calm_checkins=int(os.getenv("RISK_CALM_CHECKINS", "3")),
    ))

    # Tracking lost (tracking.py): GPS worse than this can't be judged (the detectors' own
    # GPS_MAX_ACCURACY_M), and after this long of it the caregiver is told.
    gps_max_accuracy_m: float = field(default_factory=lambda: _float("GPS_MAX_ACCURACY_M", "50"))
    tracking_lost_after_s: float = field(default_factory=lambda: _float("TRACKING_LOST_AFTER_S", "120"))

    # How long a decided tripId+seq is remembered, so a redelivered check-in isn't decided twice.
    decided_ttl_s: int = field(default_factory=lambda: int(os.getenv("DECIDED_TTL_S", "86400")))
    # Journey keys expire 24 h after their last update (contracts/redis-keys.md).
    key_ttl_s: int = field(default_factory=lambda: int(os.getenv("KEY_TTL_S", "86400")))
    # Decision records kept for the dashboard.
    decisions_per_traveller: int = field(default_factory=lambda: int(os.getenv("DECISIONS_PER_TRAVELLER", "50")))
    decisions_recent: int = field(default_factory=lambda: int(os.getenv("DECISIONS_RECENT", "200")))
    max_messages: int = field(default_factory=lambda: int(os.getenv("MAX_MESSAGES", "10")))
    consumer_enabled: bool = field(default_factory=lambda: _bool("CONSUMER_ENABLED", True))

    # --- experiment only (REDUNDANCY_MODE adaptive / fixed2, evaluation C5; adaptive.py) ---------
    # Which pair at normal risk: "step" (fits the journey step) or "fixed" (NORMAL_PAIR always).
    pair_strategy: str = field(default_factory=lambda: os.getenv("PAIR_STRATEGY", "step"))
    # On a bus/train step, slower than this counts as waiting at the stop, faster as riding.
    waiting_below_mps: float = field(default_factory=lambda: float(os.getenv("WAITING_BELOW_MPS", "2.0")))
    normal_pair: tuple[str, ...] = field(default_factory=lambda: tuple(
        d.strip() for d in os.getenv("NORMAL_PAIR", ",".join(DEFAULT_PAIR)).split(",") if d.strip()))


settings = Settings()
