"""Settings for the Coordinator, read from environment variables."""
import os
from dataclasses import dataclass, field

from .selection import DEFAULT_PAIR, parse_urls


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


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
    # unsafe failure. adaptive and fixed2 stay for the experiment (adaptive vs fixed4).
    redundancy_mode: str = field(default_factory=lambda: os.getenv("REDUNDANCY_MODE", "fixed4"))
    # Which pair at normal risk: "step" (fits the journey step) or "fixed" (NORMAL_PAIR always).
    pair_strategy: str = field(default_factory=lambda: os.getenv("PAIR_STRATEGY", "step"))
    # On a bus/train step, slower than this counts as waiting at the stop, faster as riding.
    waiting_below_mps: float = field(default_factory=lambda: float(os.getenv("WAITING_BELOW_MPS", "2.0")))
    normal_pair: tuple[str, ...] = field(default_factory=lambda: tuple(
        d.strip() for d in os.getenv("NORMAL_PAIR", ",".join(DEFAULT_PAIR)).split(",") if d.strip()))
    detector_urls: dict = field(default_factory=lambda: parse_urls(os.getenv("DETECTOR_URLS", "")))
    # contracts/api.md: detectors must answer within 500 ms.
    detector_timeout_s: float = field(default_factory=lambda: float(os.getenv("DETECTOR_TIMEOUT_MS", "500")) / 1000)
    # Fewer real (non-abstain) votes than this -> ask the remaining detectors too.
    quorum: int = field(default_factory=lambda: int(os.getenv("QUORUM", "2")))

    aggregator_url: str = field(default_factory=lambda: os.getenv("AGGREGATOR_URL", "http://aggregator:8000").rstrip("/"))
    aggregator_timeout_s: float = field(default_factory=lambda: float(os.getenv("AGGREGATOR_TIMEOUT_S", "5")))

    # How long a decided tripId+seq is remembered for dedup (journey keys live 24 h).
    decided_ttl_s: int = field(default_factory=lambda: int(os.getenv("DECIDED_TTL_S", "86400")))
    max_messages: int = field(default_factory=lambda: int(os.getenv("MAX_MESSAGES", "10")))
    consumer_enabled: bool = field(default_factory=lambda: _bool("CONSUMER_ENABLED", True))


settings = Settings()
