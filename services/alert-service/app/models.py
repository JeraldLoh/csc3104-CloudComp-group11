"""Request bodies, matching contracts/examples/."""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

TRAVELLER_ID = r"^U-[A-Za-z0-9]{1,40}$"  # contracts/README.md: U- traveller, G- caregiver
CAREGIVER_ID = r"^G-[A-Za-z0-9]{1,40}$"

# contracts/README.md: problem types used everywhere, plus "unknown": the Aggregator sends it when
# detectors agree something is wrong but none says what. Rejecting it would lose the alert.
ProblemType = Literal[
    "off_route", "wrong_direction", "missed_stop", "long_dwell", "no_response", "help_requested", "disruption",
    "unknown",
]


class Location(BaseModel):
    # contracts/README.md: unknown values are null (e.g. no GPS fix underground).
    lat: float | None = None
    lng: float | None = None


class ProblemReport(BaseModel):
    """problem-report.json (from the Aggregator) or problem-report-no-response.json (Heartbeat Monitor).

    Fields this service doesn't use (votes, detectorsUsed, phi, lastSeenAt) are accepted and ignored.
    """

    reportId: str
    source: str
    travellerId: str
    tripId: str
    stepIndex: int | None = None  # null if the check-in didn't say which step
    problemType: ProblemType
    confidence: float | None = None
    location: Location | None = None
    batteryPct: int | None = None
    timestamp: datetime  # when the problem was detected


class Disruption(BaseModel):
    """disruption.json: from the Disruption Lambda (Member 6), one message per affected trip."""

    disruptionId: str
    source: str
    tripId: str
    travellerId: str
    affectedStepIndex: int
    mode: str | None = None
    line: str | None = None
    affectedStations: list[str] = []
    message: str
    detectedAt: datetime

    def as_problem(self) -> ProblemReport:
        return ProblemReport(
            reportId=self.disruptionId,
            source=self.source,
            travellerId=self.travellerId,
            tripId=self.tripId,
            stepIndex=self.affectedStepIndex,
            problemType="disruption",
            timestamp=self.detectedAt,
        )

    def details(self) -> dict:
        """Kept on the incident so the dashboard can say what happened."""
        return {
            "disruptionId": self.disruptionId,
            "mode": self.mode,
            "line": self.line,
            "affectedStations": self.affectedStations,
            "message": self.message,
        }


# ---------------------------------------------------------------------------- profiles (issue 5.1)
class NewTraveller(BaseModel):
    """POST /api/travellers. The calling caregiver is linked to the new traveller automatically."""

    name: str = Field(min_length=1, max_length=80)
    preferences: dict[str, Any] = {}


class NewLink(BaseModel):
    """POST /api/links: add another caregiver to a traveller. Only a caregiver already linked to the
    traveller may do this, so nobody can link themselves to a stranger and see their alerts."""

    travellerId: str = Field(pattern=TRAVELLER_ID)
    caregiverId: str = Field(pattern=CAREGIVER_ID)


class NewSafeLocation(BaseModel):
    """POST /api/travellers/{travellerId}/safe-locations, e.g. Home or School."""

    name: str = Field(min_length=1, max_length=60)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
