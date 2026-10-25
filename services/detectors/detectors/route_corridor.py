"""Route-corridor detector: is the traveller far from the planned path?

Pure function, no I/O. Each check-in's allowed distance from the path is
``CORRIDOR_M`` plus that check-in's own GPS accuracy, so a fuzzy fix is never
blamed for noise it could not avoid. The vote is ``problem`` / ``off_route``
only when the last ``CORRIDOR_CONSECUTIVE`` usable check-ins are all outside
their limit, so one stray GPS reading cannot raise an alarm.
"""

from typing import List, Tuple

from detectors.common import abstain_reason, confidence as _confidence, usable_checkins
from detectors.config import Settings
from detectors.context import TripContext
from detectors.geo import nearest_segment
from detectors.models import DetectorRequest, DetectorVote

DETECTOR_ID = "route-corridor"


def _abstain(request: DetectorRequest, reason: str) -> DetectorVote:
    return DetectorVote.abstain(request.request_id, DETECTOR_ID, reason)


def detect(request: DetectorRequest, ctx: TripContext, settings: Settings) -> DetectorVote:
    reason = abstain_reason(request, ctx, settings)
    if reason:
        return _abstain(request, reason)

    needed = settings.corridor_consecutive
    # Newest first; ctx.recent already includes this request's own check-in
    usable = list(reversed(usable_checkins(ctx.recent, settings)))
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
