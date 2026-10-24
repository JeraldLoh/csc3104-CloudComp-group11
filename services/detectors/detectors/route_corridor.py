"""Route-corridor detector: is the traveller far from the planned path?

Pure function, no I/O. Each check-in's allowed distance from the path is
``CORRIDOR_M`` plus that check-in's own GPS accuracy, so a fuzzy fix is never
blamed for noise it could not avoid. The vote is ``problem`` / ``off_route``
only when the last ``CORRIDOR_CONSECUTIVE`` usable check-ins are all outside
their limit, so one stray GPS reading cannot raise an alarm.
"""

from typing import List, Tuple

from detectors.config import Settings
from detectors.context import TripContext
from detectors.geo import nearest_segment
from detectors.models import DetectorRequest, DetectorVote

DETECTOR_ID = "route-corridor"


def _abstain(request: DetectorRequest, reason: str) -> DetectorVote:
    return DetectorVote.abstain(request.request_id, DETECTOR_ID, reason)


def _confidence(score: float) -> float:
    """Map a 0..1 score onto the 0.5..0.95 confidence range."""
    return round(0.5 + 0.45 * min(1.0, max(0.0, score)), 2)


def detect(request: DetectorRequest, ctx: TripContext, settings: Settings) -> DetectorVote:
    checkin = request.checkin

    if ctx.path is None or len(ctx.path) < 2:
        return _abstain(request, "no route in Redis for this trip")
    if ctx.route_version_mismatch:
        return _abstain(
            request,
            f"route version mismatch (check-in {checkin.route_version}, trip {ctx.route_version})",
        )
    if checkin.accuracy_m is None:
        return _abstain(request, "check-in has no GPS accuracy")
    if checkin.accuracy_m > settings.gps_max_accuracy_m:
        return _abstain(
            request,
            f"GPS accuracy {checkin.accuracy_m:g} m exceeds {settings.gps_max_accuracy_m:g} m limit",
        )

    needed = settings.corridor_consecutive
    # Newest first; ctx.recent already includes this request's own check-in
    usable = [
        c
        for c in reversed(ctx.recent)
        if c.accuracy_m is not None and c.accuracy_m <= settings.gps_max_accuracy_m
    ]
    if len(usable) < needed:
        return _abstain(request, f"only {len(usable)} usable check-in(s), need {needed}")

    # (distance from path, allowed distance) per usable check-in, newest first
    measures: List[Tuple[float, float]] = []
    for c in usable:
        _, dist, _ = nearest_segment((c.lat, c.lng), ctx.path)
        measures.append((dist, settings.corridor_m + c.accuracy_m))

    run = 0  # newest consecutive check-ins that are outside their limit
    for dist, limit in measures:
        if dist <= limit:
            break
        run += 1

    dist, limit = measures[0]
    if run >= needed:
        # Further outside the limit and more agreeing check-ins both raise confidence
        excess = sum((d - lim) / lim for d, lim in measures[:run]) / run
        score = 0.5 * min(1.0, excess) + 0.5 * min(1.0, run / (2 * needed))
        return DetectorVote(
            request_id=request.request_id,
            detector_id=DETECTOR_ID,
            verdict="problem",
            problem_type="off_route",
            confidence=_confidence(score),
            reason=f"{dist:.0f} m from planned route (limit {limit:.0f} m) for {run} check-ins",
        )

    if dist > limit:
        outside = sum(1 for d, lim in measures[:needed] if d > lim)
        return DetectorVote(
            request_id=request.request_id,
            detector_id=DETECTOR_ID,
            verdict="normal",
            confidence=_confidence(0),
            reason=(
                f"{dist:.0f} m from planned route (limit {limit:.0f} m), "
                f"but only {outside} of the last {needed} check-ins are outside"
            ),
        )

    return DetectorVote(
        request_id=request.request_id,
        detector_id=DETECTOR_ID,
        verdict="normal",
        confidence=_confidence(1 - dist / limit),
        reason=f"{dist:.0f} m from planned route (limit {limit:.0f} m)",
    )
