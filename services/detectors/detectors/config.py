"""Runtime settings, read from environment variables.

Every threshold lives here so it can be tuned per Deployment without a rebuild.
Env var names are the field names in upper case, e.g. ``CORRIDOR_M=120``.
Threshold values are starting points; later stages tune them with the simulator.
"""

import os
from functools import lru_cache
from typing import Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

from detectors.models import DetectorId


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Which detector this container runs
    detector_id: Optional[DetectorId] = None
    redis_url: str = "redis://localhost:6379/0"

    # Shared: abstain when GPS is worse than this
    gps_max_accuracy_m: float = Field(default=50, gt=0)

    # Route-corridor
    corridor_m: float = Field(default=100, gt=0)
    corridor_consecutive: int = Field(default=3, ge=1)

    # Heading/speed
    min_moving_speed_mps: float = Field(default=0.5, ge=0)
    wrong_dir_deg: float = Field(default=120, gt=0, le=180)

    # Dwell-time
    dwell_radius_m: float = Field(default=30, gt=0)
    dwell_walk_s: int = Field(default=300, gt=0)
    dwell_transit_s: int = Field(default=1200, gt=0)

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "Settings":
        """Build settings from env vars named after the fields in upper case.

        Unset or empty variables fall back to the defaults above. Values are
        validated, so a typo like ``CORRIDOR_M=abc`` fails at startup.
        """
        env = os.environ if environ is None else environ
        values = {}
        for name in cls.model_fields:
            raw = env.get(name.upper())
            if raw is not None and raw.strip() != "":
                values[name] = raw.strip()
        return cls.model_validate(values)


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
