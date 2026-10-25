"""Rules every rule-based detector shares: when to abstain, and how to score confidence."""

from typing import List, Optional

from detectors.config import Settings
from detectors.context import TripContext
from detectors.models import CheckIn, DetectorRequest


def abstain_reason(request: DetectorRequest, ctx: TripContext, settings: Settings) -> Optional[str]:
    """Why the detector must not judge this request, or None if it can.

    Covers the data a detector cannot do without: the route, a matching route
    version, and a trustworthy fix for the current check-in.
    """
    checkin = request.checkin
    if ctx.path is None or len(ctx.path) < 2:
        return "no route in Redis for this trip"
    if ctx.route_version_mismatch:
        return f"route version mismatch (check-in {checkin.route_version}, trip {ctx.route_version})"
    if checkin.accuracy_m is None:
        return "check-in has no GPS accuracy"
    if checkin.accuracy_m > settings.gps_max_accuracy_m:
        return f"GPS accuracy {checkin.accuracy_m:g} m exceeds {settings.gps_max_accuracy_m:g} m limit"
    return None


def usable_checkins(checkins: List[CheckIn], settings: Settings) -> List[CheckIn]:
    """The check-ins with a trustworthy fix, in the same order."""
    return [
        c for c in checkins if c.accuracy_m is not None and c.accuracy_m <= settings.gps_max_accuracy_m
    ]


def confidence(score: float) -> float:
    """Map a 0..1 score onto the 0.5..0.95 confidence range."""
    return round(0.5 + 0.45 * min(1.0, max(0.0, score)), 2)
