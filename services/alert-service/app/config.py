"""Settings for the Alert Service, read from environment variables."""
import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    # LocalStack locally (http://localhost:4566). Unset on AWS, so boto3 talks to real AWS.
    aws_endpoint_url: str | None = field(default_factory=lambda: os.getenv("AWS_ENDPOINT_URL") or None)
    aws_region: str = field(default_factory=lambda: os.getenv("AWS_DEFAULT_REGION", "ap-southeast-1"))

    # Table names from contracts/dynamo-db-tables.md
    incidents_table: str = field(default_factory=lambda: os.getenv("INCIDENTS_TABLE", "careroute-incidents"))
    travellers_table: str = field(default_factory=lambda: os.getenv("TRAVELLERS_TABLE", "careroute-travellers"))
    caregiver_links_table: str = field(default_factory=lambda: os.getenv("CAREGIVER_LINKS_TABLE", "careroute-caregiver-links"))
    safe_locations_table: str = field(default_factory=lambda: os.getenv("SAFE_LOCATIONS_TABLE", "careroute-safe-locations"))
    device_tokens_table: str = field(default_factory=lambda: os.getenv("DEVICE_TOKENS_TABLE", "careroute-device-tokens"))

    # contracts/queues.md. On AWS the deploy script sets DISRUPTIONS_QUEUE_URL from the platform stack;
    # locally the URL is looked up by name.
    disruptions_queue: str = field(default_factory=lambda: os.getenv("DISRUPTIONS_QUEUE", "careroute-disruptions"))
    disruptions_queue_url: str | None = field(default_factory=lambda: os.getenv("DISRUPTIONS_QUEUE_URL") or None)
    disruptions_consumer_enabled: bool = field(default_factory=lambda: _bool("DISRUPTIONS_CONSUMER_ENABLED", True))

    # Create the tables and queue on startup. Local only: on AWS the platform stack already has them.
    create_resources: bool = field(default_factory=lambda: _bool("CREATE_RESOURCES", False))


settings = Settings()
