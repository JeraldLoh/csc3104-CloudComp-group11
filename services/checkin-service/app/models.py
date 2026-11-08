"""Request bodies, matching contracts/examples/checkin.json and checkin-batch.json."""
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import settings

TRIP_ID = r"^T-[A-Za-z0-9-]+$"
TRAVELLER_ID = r"^U-[A-Za-z0-9-]+$"


class CheckinFields(BaseModel):
    """Fields every check-in has. In a batch, tripId and travellerId sit on the batch instead."""

    # Contracts allow new optional fields later, so unknown fields are ignored, not rejected.
    model_config = ConfigDict(extra="ignore")

    seq: int = Field(ge=0)
    timestamp: datetime
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracyM: float | None = Field(default=None, ge=0)
    speedMps: float | None = Field(default=None, ge=0)
    headingDeg: float | None = Field(default=None, ge=0, lt=360)
    stepIndex: int | None = Field(default=None, ge=0)
    routeVersion: int | None = Field(default=None, ge=0)
    batteryPct: float | None = Field(default=None, ge=0, le=100)
    status: Literal["active", "backgrounded"] = "active"
    bufferedOffline: bool = False

    @field_validator("timestamp")
    @classmethod
    def utc_and_not_in_future(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp must include a timezone, e.g. 2026-10-12T08:15:30Z")
        v = v.astimezone(timezone.utc)
        if v > datetime.now(timezone.utc) + timedelta(seconds=settings.max_future_skew_s):
            raise ValueError("timestamp is in the future; check the phone's clock")
        return v


class Checkin(CheckinFields):
    tripId: str = Field(pattern=TRIP_ID)
    travellerId: str = Field(pattern=TRAVELLER_ID)

    def to_contract(self) -> dict:
        """JSON-ready dict in contract format (camelCase, UTC times ending in Z)."""
        d = self.model_dump()
        d["timestamp"] = iso_z(self.timestamp)
        return d


class CheckinBatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tripId: str = Field(pattern=TRIP_ID)
    travellerId: str = Field(pattern=TRAVELLER_ID)
    checkins: list[CheckinFields] = Field(min_length=1)

    @field_validator("checkins")
    @classmethod
    def not_too_many(cls, v: list) -> list:
        if len(v) > settings.max_batch:
            raise ValueError(f"at most {settings.max_batch} check-ins per batch")
        return v

    def expand(self) -> list[Checkin]:
        """Full check-ins, oldest first, so they are applied in the order they happened."""
        items = [
            Checkin(tripId=self.tripId, travellerId=self.travellerId, **c.model_dump())
            for c in self.checkins
        ]
        return sorted(items, key=lambda c: c.seq)


def iso_ms(dt: datetime) -> str:
    """2026-10-12T08:15:30.000Z: always milliseconds, so stored times sort correctly as text."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def iso_z(dt: datetime) -> str:
    """2026-10-12T08:15:30Z (contract time format; milliseconds kept if present)."""
    dt = dt.astimezone(timezone.utc)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
