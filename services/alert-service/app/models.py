"""Request bodies, matching contracts/examples/."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

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
