"""Settings for the Check-in Service, read from environment variables."""
import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    # Unset in real AWS; set to LocalStack (http://cloud-localstack:4566) locally.
    aws_endpoint_url: str | None = field(default_factory=lambda: os.getenv("AWS_ENDPOINT_URL") or None)
    aws_region: str = field(default_factory=lambda: os.getenv("AWS_DEFAULT_REGION", "ap-southeast-1"))

    table_name: str = field(default_factory=lambda: os.getenv("CHECKINS_TABLE", "careroute-checkins"))
    queue_name: str = field(default_factory=lambda: os.getenv("CHECKINS_QUEUE", "careroute-checkins"))

    # contracts/dynamo-db-tables.md: delete check-ins 7 days later.
    checkin_ttl_days: int = field(default_factory=lambda: int(os.getenv("CHECKIN_TTL_DAYS", "7")))
    # contracts/redis-keys.md: journey keys expire 24 h after their last update.
    trip_key_ttl_s: int = field(default_factory=lambda: int(os.getenv("TRIP_KEY_TTL_S", "86400")))
    recent_max: int = 10        # trip:{tripId}:recent keeps the last 10
    intervals_max: int = 100    # hb:{tripId}:intervals keeps the last 100

    max_batch: int = field(default_factory=lambda: int(os.getenv("MAX_BATCH", "500")))
    # Reject check-ins stamped further in the future than this (bad phone clock).
    max_future_skew_s: int = field(default_factory=lambda: int(os.getenv("MAX_FUTURE_SKEW_S", "300")))

    # Create the table and queues on startup. For local development only;
    # in AWS they are created once with infra/create_resources.py.
    create_resources: bool = field(default_factory=lambda: _bool("CREATE_RESOURCES", False))
    # Login (Cognito, issue 1.5) isn't built yet. When true, a Bearer token is required.
    auth_required: bool = field(default_factory=lambda: _bool("AUTH_REQUIRED", False))


settings = Settings()
