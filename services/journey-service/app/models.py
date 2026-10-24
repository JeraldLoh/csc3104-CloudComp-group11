"""Request bodies. Field names follow contracts/README.md (camelCase, ISO 8601 times)."""
from datetime import date as date_type, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LatLng(BaseModel):
    # Rough bounding box of Singapore; OneMap only covers it, so reject anything else early.
    lat: float = Field(ge=1.1, le=1.5)
    lng: float = Field(ge=103.5, le=104.1)


class PlanRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: LatLng = Field(alias="from")
    to: LatLng
    # Optional departure time. No timezone means Singapore time. Default: now.
    departAt: datetime | None = None
    # "any" = buses and trains, "bus" = buses only, "train" = MRT/LRT only.
    mode: Literal["any", "bus", "train"] = "any"
    # Longest single walk OneMap may use, in metres (default from config). Lower = less walking.
    maxWalkM: int | None = Field(default=None, ge=100, le=2000)
    maxOptions: int | None = Field(default=None, ge=1, le=5)
    # Option order: "walking" = least walking first, "crowding" = least crowded first (then least walking),
    # "time" = OneMap's own order. Ties keep OneMap's order.
    sortBy: Literal["walking", "crowding", "time"] = "walking"


# --- saved routes (contracts/examples/route.json) -------------------------------------
# Unknown top-level fields are ignored (so a plan option can be posted as-is); unknown step
# fields are kept (contracts/README.md: adding an optional field is safe, e.g. a train step's `stops`).

class Place(BaseModel):
    name: str | None = None
    safeLocationId: str | None = None
    lat: float = Field(ge=1.1, le=1.5)
    lng: float = Field(ge=103.5, le=104.1)


class Stop(BaseModel):
    name: str | None = None
    code: str | None = None
    lat: float | None = None
    lng: float | None = None


class Backup(BaseModel):
    instruction: str
    line: str | None = None


class Step(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    stepIndex: int = Field(ge=0)
    mode: Literal["walk", "bus", "train"]
    instruction: str = Field(min_length=1, max_length=500)
    easyRead: str | None = None
    from_: Stop = Field(alias="from")
    to: Stop
    line: str | None = None
    expectedDurationS: int | None = Field(default=None, ge=0)
    underground: bool | None = None
    photoKey: str | None = None
    audioKey: str | None = None
    backup: Backup | None = None


class RouteIn(BaseModel):
    """Body of POST /api/routes: route.json without routeId (the server adds routeId, version, approved)."""
    model_config = ConfigDict(populate_by_name=True)

    travellerId: str = Field(min_length=1, max_length=64)
    caregiverId: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    origin: Place
    destination: Place
    steps: list[Step] = Field(min_length=1, max_length=50)
    path: list[tuple[float, float]] = Field(default_factory=list, max_length=5000)

    @model_validator(mode="after")
    def _steps_in_order(self):
        if [s.stepIndex for s in self.steps] != list(range(len(self.steps))):
            raise ValueError("steps[].stepIndex must be 0, 1, 2, ... in order")
        return self


class TripIn(BaseModel):
    """Body of POST /api/trips."""
    routeId: str = Field(pattern=r"^R-[0-9A-F]{6}$")
    date: date_type                       # "2026-10-12"


class RouteUpdate(RouteIn):
    """Body of PUT /api/routes/{id}: the route as you last saw it, with your changes. `version` is the
    version you edited; if someone saved in between the server answers 409 instead of overwriting."""
    version: int = Field(ge=1)
