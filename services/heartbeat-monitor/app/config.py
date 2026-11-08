"""Settings for the Heartbeat Monitor, read from environment variables."""
import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    # contracts/api.md: services call each other by name inside the cluster.
    alert_service_url: str = field(default_factory=lambda: os.getenv("ALERT_SERVICE_URL", "http://alert-service:8000"))
    alert_timeout_s: float = field(default_factory=lambda: _float("ALERT_TIMEOUT_S", 3))

    # How often every active trip is checked. The app checks in every 10 s, so 5 s keeps the delay
    # between crossing the threshold and alerting small.
    tick_s: float = field(default_factory=lambda: _float("TICK_S", 5))

    # φ above this = suspected unreachable. 8 is Akka's default: a 1 in 100 million chance that the
    # phone is merely late, given its rhythm (contracts/examples/problem-report-no-response.json: 8.4).
    phi_threshold: float = field(default_factory=lambda: _float("PHI_THRESHOLD", 8.0))
    # The app's check-in rhythm (apps/traveller tracker.ts), used until a trip has its own history.
    expected_interval_ms: float = field(default_factory=lambda: _float("EXPECTED_INTERVAL_MS", 10_000))
    min_samples: int = field(default_factory=lambda: int(os.getenv("MIN_SAMPLES", "5")))
    # Phones on mobile data are never perfectly regular; this stops one slightly late check-in after a
    # very regular run from looking like a failure.
    min_std_ms: float = field(default_factory=lambda: _float("MIN_STD_MS", 2_000))

    # Silence we expect, added to the learnt mean gap (seconds). With the defaults and a 10 s rhythm,
    # φ reaches 8 after roughly 50 s of silence above ground, 5.8 min on an underground step and
    # 15.8 min with the app in the background.
    base_pause_s: float = field(default_factory=lambda: _float("BASE_PAUSE_S", 30))
    # Extra for an underground step: no GPS, patchy signal between stations.
    underground_pause_s: float = field(default_factory=lambda: _float("UNDERGROUND_PAUSE_S", 300))
    # Extra once the app has said it's going into the background (status "backgrounded"): the browser
    # slows or stops it, so silence is expected, but not forever.
    backgrounded_pause_s: float = field(default_factory=lambda: _float("BACKGROUNDED_PAUSE_S", 900))

    # contracts/redis-keys.md: journey keys expire 24 h after their last update.
    trip_key_ttl_s: int = field(default_factory=lambda: int(os.getenv("TRIP_KEY_TTL_S", "86400")))
    # Only one replica scans at a time (leader lease, see lease.py). Another replica takes over within
    # about this + one tick if the leader dies.
    leader_lease_ms: int = field(default_factory=lambda: int(os.getenv("LEADER_LEASE_MS", "15000")))
    monitor_enabled: bool = field(default_factory=lambda: _bool("MONITOR_ENABLED", True))


settings = Settings()
