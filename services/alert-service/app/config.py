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

    # The Firebase service account key file (push.py). Empty or missing = no push notifications.
    firebase_credentials: str = field(default_factory=lambda: os.getenv("FIREBASE_CREDENTIALS", ""))

    # Escalation (escalation.py): an urgent alert still open this long after it opened, or after its last
    # escalation, is sent again, up to ESCALATE_MAX times. Checked every ESCALATION_CHECK_S.
    escalation_enabled: bool = field(default_factory=lambda: _bool("ESCALATION_ENABLED", True))
    escalate_after_s: int = field(default_factory=lambda: int(os.getenv("ESCALATE_AFTER_S", "120")))
    escalate_max: int = field(default_factory=lambda: int(os.getenv("ESCALATE_MAX", "3")))
    escalation_check_s: float = field(default_factory=lambda: float(os.getenv("ESCALATION_CHECK_S", "15")))
    escalate_within_s: int = field(default_factory=lambda: int(os.getenv("ESCALATE_WITHIN_S", "3600")))  # recent only
    # An important alert is only sent again while it's still happening (levels.py): reported again this recently,
    # or for long_dwell, the phone's latest check-in still within this many metres of where they stopped.
    escalate_still_detected_s: int = field(default_factory=lambda: int(os.getenv("ESCALATE_STILL_DETECTED_S", "60")))
    dwell_radius_m: float = field(default_factory=lambda: float(os.getenv("DWELL_RADIUS_M", "30")))

    # A trip counts as live until its last check-in is this old (live.py).
    live_window_s: int = field(default_factory=lambda: int(os.getenv("LIVE_WINDOW_S", "1800")))

    # Create the tables and queue on startup. Local only: on AWS the platform stack already has them.
    create_resources: bool = field(default_factory=lambda: _bool("CREATE_RESOURCES", False))

    # Who is calling (auth.py). Same settings as the Journey Service, so both services identify callers alike.
    # AUTH_MODE: "off" = nobody is checked (the default), "dev" = trust an X-Dev-User header (local testing
    # only, never deploy it), "cognito" = verify a Cognito JWT (issue 1.5).
    auth_mode: str = field(default_factory=lambda: os.getenv("AUTH_MODE", "off").strip().lower())
    auth_id_claim: str = field(default_factory=lambda: os.getenv("AUTH_ID_CLAIM", "custom:userId"))
    auth_role_claim: str = field(default_factory=lambda: os.getenv("AUTH_ROLE_CLAIM", "cognito:groups"))
    auth_caregiver_group: str = field(default_factory=lambda: os.getenv("AUTH_CAREGIVER_GROUP", "caregiver"))
    auth_traveller_group: str = field(default_factory=lambda: os.getenv("AUTH_TRAVELLER_GROUP", "traveller"))
    auth_token_use: str = field(default_factory=lambda: os.getenv("AUTH_TOKEN_USE", "id").strip().lower())
    cognito_region: str = field(default_factory=lambda: os.getenv("COGNITO_REGION", ""))
    cognito_user_pool_id: str = field(default_factory=lambda: os.getenv("COGNITO_USER_POOL_ID", ""))
    cognito_client_id: str = field(default_factory=lambda: os.getenv("COGNITO_CLIENT_ID", ""))
    cognito_jwks_url: str = field(default_factory=lambda: os.getenv("COGNITO_JWKS_URL", ""))


settings = Settings()
