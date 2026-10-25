"""Pydantic models for the detector contracts.

These mirror the JSON examples in ``contracts/examples``:

- ``checkin.json``           -> :class:`CheckIn`
- ``detector-request.json``  -> :class:`DetectorRequest`
- ``detector-vote.json``     -> :class:`DetectorVote`

Python attributes are snake_case; JSON is camelCase (see contracts/README.md).
Always dump with ``model_dump(by_alias=True, mode="json")`` or
``model_dump_json(by_alias=True)`` so other services get camelCase keys.
"""

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


ProblemType = Literal[
    "off_route",
    "wrong_direction",
    "missed_stop",
    "long_dwell",
    "no_response",
    "help_requested",
    "disruption",
]

# "abstain" = the detector can't decide (bad GPS, missing route, too little data,
# internal error). See contracts/api.md, Detectors section.
Verdict = Literal["normal", "problem", "abstain"]

DetectorId = Literal["route-corridor", "heading-speed", "dwell-time", "ml-anomaly"]

RiskLevel = Literal["normal", "elevated"]


class ContractModel(BaseModel):
    """Base for all contract models: camelCase aliases, tolerant of new fields.

    ``extra="ignore"`` because the contract allows other members to add new
    optional fields at any time without breaking receivers.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
    )


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone (ISO 8601 ending in Z)")
    return value.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    # Contract: ISO 8601 UTC ending in "Z", e.g. 2026-10-12T08:15:30Z
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class CheckIn(ContractModel):
    trip_id: str
    traveller_id: str
    seq: int = Field(ge=0)
    timestamp: datetime
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    # Nullable: offline/buffered check-ins may lack speed and heading
    accuracy_m: Optional[float] = Field(default=None, ge=0)
    speed_mps: Optional[float] = Field(default=None, ge=0)
    heading_deg: Optional[float] = Field(default=None, ge=0, lt=360)
    step_index: int = Field(ge=0)
    route_version: int = Field(ge=0)
    battery_pct: Optional[int] = Field(default=None, ge=0, le=100)
    status: str
    # Not present inside detector-request.json, so default to False.
    buffered_offline: bool = False

    @field_validator("timestamp")
    @classmethod
    def _check_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_serializer("timestamp")
    def _ser_timestamp(self, value: datetime) -> str:
        return _format_utc(value)


class DetectorRequest(ContractModel):
    """Sent by the Coordinator (M3) to POST /internal/detect."""

    request_id: str
    risk_level: RiskLevel
    checkin: CheckIn


class DetectorVote(ContractModel):
    """Returned by a detector to the Coordinator (M3)."""

    request_id: str
    detector_id: DetectorId
    verdict: Verdict
    problem_type: Optional[ProblemType] = None
    confidence: float = Field(ge=0, le=1)
    reason: str
    latency_ms: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check_verdict_consistency(self) -> "DetectorVote":
        if self.verdict == "problem" and self.problem_type is None:
            raise ValueError("verdict 'problem' requires a problemType")
        if self.verdict != "problem" and self.problem_type is not None:
            raise ValueError(f"verdict '{self.verdict}' must have problemType null")
        if self.verdict == "abstain" and self.confidence != 0:
            raise ValueError("verdict 'abstain' must have confidence 0")
        return self

    @classmethod
    def abstain(
        cls,
        request_id: str,
        detector_id: DetectorId,
        reason: str,
        latency_ms: float = 0,
    ) -> "DetectorVote":
        """Convenience constructor for the 'can't decide' case."""
        return cls(
            request_id=request_id,
            detector_id=detector_id,
            verdict="abstain",
            problem_type=None,
            confidence=0.0,
            reason=reason,
            latency_ms=latency_ms,
        )
