from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel


# problem types shared by all services
ProblemType = Literal[
    "off_route",
    "wrong_direction",
    "missed_stop",
    "long_dwell",
    "no_response",
    "help_requested",
    "disruption",
]

# abstain means the detector cannot decide
Verdict = Literal["normal", "problem", "abstain"]

DetectorId = Literal["route-corridor", "heading-speed", "dwell-time", "route-progress", "ml-anomaly"]

RiskLevel = Literal["normal", "elevated"]


# camelCase on the wire and ignores unknown fields
class ContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
    )


# timestamps must carry a timezone
def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone (ISO 8601 ending in Z)")
    return value.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    # iso 8601 utc ending in Z
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# one gps check-in from the traveller app
class CheckIn(ContractModel):
    trip_id: str
    traveller_id: str
    seq: int = Field(ge=0)
    timestamp: datetime
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    # offline check-ins may lack speed and heading
    accuracy_m: Optional[float] = Field(default=None, ge=0)
    speed_mps: Optional[float] = Field(default=None, ge=0)
    heading_deg: Optional[float] = Field(default=None, ge=0, lt=360)
    # web app sends null until the route pack loads
    step_index: Optional[int] = Field(default=None, ge=0)
    route_version: Optional[int] = Field(default=None, ge=0)
    battery_pct: Optional[int] = Field(default=None, ge=0, le=100)
    status: str
    # not in the detector request so defaults to False
    buffered_offline: bool = False

    @field_validator("timestamp")
    @classmethod
    def _check_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_serializer("timestamp")
    def _ser_timestamp(self, value: datetime) -> str:
        return _format_utc(value)


# sent by the coordinator
class DetectorRequest(ContractModel):
    request_id: str
    risk_level: RiskLevel
    checkin: CheckIn


# returned to the coordinator
class DetectorVote(ContractModel):
    request_id: str
    detector_id: DetectorId
    verdict: Verdict
    problem_type: Optional[ProblemType] = None
    confidence: float = Field(ge=0, le=1)
    reason: str
    latency_ms: float = Field(default=0, ge=0)

    # problem needs a type and abstain has zero confidence
    @model_validator(mode="after")
    def _check_verdict_consistency(self) -> "DetectorVote":
        if self.verdict == "problem" and self.problem_type is None:
            raise ValueError("verdict 'problem' requires a problemType")
        if self.verdict != "problem" and self.problem_type is not None:
            raise ValueError(f"verdict '{self.verdict}' must have problemType null")
        if self.verdict == "abstain" and self.confidence != 0:
            raise ValueError("verdict 'abstain' must have confidence 0")
        return self

    # shortcut for a cannot decide vote
    @classmethod
    def abstain(
        cls,
        request_id: str,
        detector_id: DetectorId,
        reason: str,
        latency_ms: float = 0,
    ) -> "DetectorVote":
        return cls(
            request_id=request_id,
            detector_id=detector_id,
            verdict="abstain",
            problem_type=None,
            confidence=0.0,
            reason=reason,
            latency_ms=latency_ms,
        )
