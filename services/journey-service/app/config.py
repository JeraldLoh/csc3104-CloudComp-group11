# To be on AWS Lambda, the service needs only the files in app/ (plus config.py, cache.py, lta.py, disruption.py and aws_resources.py). 

"""Settings for the Journey Service, read from environment variables."""
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

    # LTA DataMall. The key is issued per subscriber; never log it or send it to the browser.
    lta_account_key: str = field(default_factory=lambda: os.getenv("LTA_API_KEY", ""))
    lta_base_url: str = field(default_factory=lambda: os.getenv(
        "LTA_BASE_URL", "https://datamall2.mytransport.sg/ltaodataservice"))
    lta_timeout_s: float = field(default_factory=lambda: float(os.getenv("LTA_TIMEOUT_S", "5")))
    lta_retries: int = field(default_factory=lambda: int(os.getenv("LTA_RETRIES", "2")))

    # OneMap. With the account's email and password the service logs in and renews its own token (onemap_token.py);
    # ONEMAP_API_KEY is a pasted token (a JWT that lasts 3 days), used when there is no account or the login fails.
    onemap_email: str = field(default_factory=lambda: os.getenv("ONEMAP_API_EMAIL", ""))
    onemap_password: str = field(default_factory=lambda: os.getenv("ONEMAP_API_PASSWORD", ""), repr=False)
    onemap_api_key: str = field(default_factory=lambda: os.getenv("ONEMAP_API_KEY", ""), repr=False)
    onemap_base_url: str = field(default_factory=lambda: os.getenv("ONEMAP_BASE_URL", "https://www.onemap.gov.sg"))
    onemap_timeout_s: float = field(default_factory=lambda: float(os.getenv("ONEMAP_TIMEOUT_S", "15")))
    onemap_retries: int = field(default_factory=lambda: int(os.getenv("ONEMAP_RETRIES", "1")))
    # Local only: after each login, write the new token into this .env file as ONEMAP_API_KEY, so tools that read the
    # .env have a fresh one. Unset (the default, and on AWS) = never written.
    onemap_token_env_file: str = field(default_factory=lambda: os.getenv("ONEMAP_TOKEN_ENV_FILE", ""))

    # "recorded" (the default) answers from the responses saved in app/saved_data/ by scripts/capture_data.py (no
    # internet and no keys needed); "live" calls the real APIs, and AWS always sets it. See replay.py. With DEMO_UI the
    # mode can also be switched at runtime (data_mode.py); these are then only the starting values.
    onemap_mode: str = field(default_factory=lambda: os.getenv("ONEMAP_MODE", "recorded").strip().lower())
    lta_mode: str = field(default_factory=lambda: os.getenv("LTA_MODE", "recorded").strip().lower())

    places_ttl_s: int = field(default_factory=lambda: int(os.getenv("PLACES_TTL_S", "3600")))

    # Planner: how many options to return and what counts as "near" a road incident.
    plan_options: int = field(default_factory=lambda: int(os.getenv("PLAN_OPTIONS", "3")))
    # Longest single walk before a long_walk warning (a whole trip may walk twice this). 500 m is a lot for the
    # people CareRoute is for; a request can raise or lower it with maxWalkM.
    plan_max_walk_m: int = field(default_factory=lambda: int(os.getenv("PLAN_MAX_WALK_M", "500")))
    incident_radius_m: float = field(default_factory=lambda: float(os.getenv("INCIDENT_RADIUS_M", "200")))

    # Saved routes. "redis" needs no AWS (demos, tests); "dynamodb" is the real store
    # (table from contracts/dynamo-db-tables.md; LocalStack locally, real DynamoDB on AWS).
    routes_store: str = field(default_factory=lambda: os.getenv("ROUTES_STORE", "redis").strip().lower())
    routes_table: str = field(default_factory=lambda: os.getenv("ROUTES_TABLE", "careroute-routes"))

    # /ready remembers a successful storage check for this long, so a burst of probes doesn't each call AWS.
    # Failures are never remembered: a recovered pod becomes ready on the next probe.
    ready_cache_s: float = field(default_factory=lambda: float(os.getenv("READY_CACHE_S", "10")))

    # Trips use the same ROUTES_STORE setting. The route cache for an active trip expires after 24 h
    # (contracts/redis-keys.md: journey keys expire 24 hours after their last update).
    trips_table: str = field(default_factory=lambda: os.getenv("TRIPS_TABLE", "careroute-trips"))
    trip_key_ttl_s: int = field(default_factory=lambda: int(os.getenv("TRIP_KEY_TTL_S", "86400")))

    # Route packs in S3. Off unless PACKS_BUCKET is set (e.g. careroute-packs), so a deployment without a
    # bucket still becomes ready. S3_PUBLIC_ENDPOINT_URL is the address a *browser* uses to reach S3 when it
    # differs from AWS_ENDPOINT_URL (LocalStack: http://localhost:4566).
    packs_bucket: str = field(default_factory=lambda: os.getenv("PACKS_BUCKET", ""))
    pack_url_ttl_s: int = field(default_factory=lambda: int(os.getenv("PACK_URL_TTL_S", "3600")))
    s3_public_endpoint_url: str | None = field(default_factory=lambda: os.getenv("S3_PUBLIC_ENDPOINT_URL") or None)

    # AI easy-read: the Easy-read Service (services/easyread-service, which runs Ollama). Only the caregiver's
    # "Simplify with AI" button calls it. Unset = always the template wording. CPU inference is slow, hence the timeout.
    easyread_url: str = field(default_factory=lambda: os.getenv("EASYREAD_URL", ""))
    easyread_timeout_s: float = field(default_factory=lambda: float(os.getenv("EASYREAD_TIMEOUT_S", "90")))
    # Landmarks along the route (landmarks.py, from OneMap) for the AI to mention, so the traveller can tell they
    # are going the right way. Off = the AI only rewords the steps.
    easyread_landmarks: bool = field(default_factory=lambda: _bool("EASYREAD_LANDMARKS", True))

    # Landmark photos (streetview.py), stored in PACKS_BUCKET under media/. Unset key = no Street View photos;
    # caregivers can still upload their own. Like the LTA key: never log it or send it to the browser.
    google_maps_api_key: str = field(default_factory=lambda: os.getenv("GOOGLE_MAPS_API_KEY", ""), repr=False)
    streetview_timeout_s: float = field(default_factory=lambda: float(os.getenv("STREETVIEW_TIMEOUT_S", "5")))
    # Older Street View panoramas are skipped: in Singapore the old Google ones are mostly indoor walk-throughs.
    streetview_max_age_years: int = field(default_factory=lambda: int(os.getenv("STREETVIEW_MAX_AGE_YEARS", "5")))
    # Photo links in packs must last while the phone is offline: 7 days is the longest an S3 presigned link allows.
    photo_url_ttl_s: int = field(default_factory=lambda: int(os.getenv("PHOTO_URL_TTL_S", "604800")))
    photo_max_bytes: int = field(default_factory=lambda: int(os.getenv("PHOTO_MAX_BYTES", str(5 * 1024 * 1024))))

    # Disruption checker (Lambda). contracts/queues.md: the sender (us) creates this queue.
    disruptions_queue: str = field(default_factory=lambda: os.getenv("DISRUPTIONS_QUEUE", "careroute-disruptions"))
    # LTA's Status is 1 for normal/minor delays and 2 for disrupted/major delays. Alerting on
    # minor delays would cause the false alarms CareRoute is meant to avoid, so default to 2.
    disruption_min_status: int = field(default_factory=lambda: int(os.getenv("DISRUPTION_MIN_STATUS", "2")))
    # Don't re-send the same disruption for the same trip within this window.
    disruption_dedupe_ttl_s: int = field(default_factory=lambda: int(os.getenv("DISRUPTION_DEDUPE_TTL_S", "21600")))
    # Create the routes table and queue on startup. For local development only; in AWS they are created once.
    create_resources: bool = field(default_factory=lambda: _bool("CREATE_RESOURCES", False))

    # Cache lifetimes follow the refresh rates in the LTA DataMall guide (v6.10).
    bus_stops_ttl_s: int = field(default_factory=lambda: int(os.getenv("BUS_STOPS_TTL_S", "86400")))
    bus_arrival_ttl_s: int = field(default_factory=lambda: int(os.getenv("BUS_ARRIVAL_TTL_S", "20")))
    train_alerts_ttl_s: int = field(default_factory=lambda: int(os.getenv("TRAIN_ALERTS_TTL_S", "60")))
    traffic_incidents_ttl_s: int = field(default_factory=lambda: int(os.getenv("TRAFFIC_INCIDENTS_TTL_S", "120")))
    # Platform crowding: real-time levels refresh every 10 minutes, the forecast once a day.
    platform_crowd_ttl_s: int = field(default_factory=lambda: int(os.getenv("PLATFORM_CROWD_TTL_S", "120")))
    platform_forecast_ttl_s: int = field(default_factory=lambda: int(os.getenv("PLATFORM_FORECAST_TTL_S", "21600")))

    # Local demo extras: the live/saved data switch (/api/dev/data-mode) and app/demo_tools.py. Neither has a login,
    # so leave it off outside local demos.
    demo_ui: bool = field(default_factory=lambda: _bool("DEMO_UI", False))

    # Who is calling (see auth.py). AUTH_MODE: "off" = nobody is checked (local demos, the default),
    # "dev" = trust an X-Dev-User header (local testing only, never deploy it), "cognito" = verify a Cognito JWT.
    auth_mode: str = field(default_factory=lambda: os.getenv("AUTH_MODE", "off").strip().lower())
    # Which claims carry the caller's contract ID (G-003 / U-042) and role. Cognito's custom attributes appear in the
    # ID token as "custom:<name>", and its groups as "cognito:groups". Names are settings because the team hasn't
    # fixed them yet; changing them is a config change.
    auth_id_claim: str = field(default_factory=lambda: os.getenv("AUTH_ID_CLAIM", "custom:userId"))
    auth_role_claim: str = field(default_factory=lambda: os.getenv("AUTH_ROLE_CLAIM", "cognito:groups"))
    auth_caregiver_group: str = field(default_factory=lambda: os.getenv("AUTH_CAREGIVER_GROUP", "caregiver"))
    auth_traveller_group: str = field(default_factory=lambda: os.getenv("AUTH_TRAVELLER_GROUP", "traveller"))
    # "id" or "access": which Cognito token the apps send. Custom attributes only exist in the ID token.
    auth_token_use: str = field(default_factory=lambda: os.getenv("AUTH_TOKEN_USE", "id").strip().lower())
    cognito_region: str = field(default_factory=lambda: os.getenv("COGNITO_REGION", ""))
    cognito_user_pool_id: str = field(default_factory=lambda: os.getenv("COGNITO_USER_POOL_ID", ""))
    cognito_client_id: str = field(default_factory=lambda: os.getenv("COGNITO_CLIENT_ID", ""))
    # Defaults to the pool's standard address; set it only to point at something else.
    cognito_jwks_url: str = field(default_factory=lambda: os.getenv("COGNITO_JWKS_URL", ""))
    # Web addresses allowed to call this API from a browser (CORS), comma-separated.
    cors_origins: tuple[str, ...] = field(default_factory=lambda: tuple(
        o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()))

    def __post_init__(self):
        for name in ("onemap_mode", "lta_mode"):
            if getattr(self, name) not in ("live", "recorded"):
                raise ValueError(f"{name.upper()} must be 'live' or 'recorded', not {getattr(self, name)!r}")


settings = Settings()
