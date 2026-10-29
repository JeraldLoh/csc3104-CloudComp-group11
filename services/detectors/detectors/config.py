import os
from functools import lru_cache
from typing import Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

from detectors.models import DetectorId


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # detector this container runs
    detector_id: Optional[DetectorId] = None
    redis_url: str = "redis://localhost:6379/0"

    # abstain when gps is worse than this
    gps_max_accuracy_m: float = Field(default=50, gt=0)

    # route corridor
    corridor_m: float = Field(default=100, gt=0)
    corridor_consecutive: int = Field(default=3, ge=1)

    # heading speed
    min_moving_speed_mps: float = Field(default=0.5, ge=0)
    wrong_dir_deg: float = Field(default=120, gt=0, le=180)
    # faster than this on a walk step is only noted in the reason
    walk_max_speed_mps: float = Field(default=4.0, gt=0)

    # dwell time
    dwell_radius_m: float = Field(default=30, gt=0)
    dwell_walk_s: int = Field(default=300, gt=0)
    dwell_transit_s: int = Field(default=1200, gt=0)

    # route progress also uses corridor_m corridor_consecutive and walk_max_speed_mps
    # less than this on top of gps accuracy is not progress
    progress_min_m: float = Field(default=20, ge=0)
    # this far past the stop at vehicle speed is a missed stop
    missed_stop_m: float = Field(default=150, gt=0)
    # fastest believable speed between check-ins
    progress_max_speed_mps: float = Field(default=30, gt=0)
    # low confidence normal so a detour cannot outvote route corridor
    progress_unsure_confidence: float = Field(default=0.3, ge=0, le=1)

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "Settings":
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
