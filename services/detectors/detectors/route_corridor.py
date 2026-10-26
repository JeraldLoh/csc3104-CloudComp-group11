from detectors.common import abstain_reason, confidence as _confidence, usable_checkins
from detectors.geo import nearest_segment
from detectors.models import DetectorVote

DETECTOR_ID = "route-corridor"


def _abstain(request, reason):
    return DetectorVote.abstain(request.request_id, DETECTOR_ID, reason)


# off route when the newest check-ins in a row are all outside the corridor
def detect(request, ctx, settings):
    reason = abstain_reason(request, ctx, settings)
    if reason:
        return _abstain(request, reason)

    # check-ins in a row needed before calling it off route
    needed = settings.corridor_consecutive
    # newest first and includes this check-in
    usable = list(reversed(usable_checkins(ctx.recent, settings)))
    if len(usable) < needed:
        return _abstain(request, f"only {len(usable)} usable check-in(s), need {needed}")

    # distance from path and allowed distance per check-in newest first
    measures = []
    for c in usable:
        # limit grows with gps accuracy so a fuzzy fix is not blamed
        _, dist, _ = nearest_segment((c.lat, c.lng), ctx.path)
        measures.append((dist, settings.corridor_m + c.accuracy_m))

    run = 0  # newest check-ins in a row outside their limit
    for dist, limit in measures:
        if dist <= limit:
            break
        run += 1

    # newest check-in drives the reason text
    dist, limit = measures[0]
    if run >= needed:
        # further outside and more agreeing check-ins raise confidence
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

    # newest is outside but not enough in a row yet
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

    # inside the corridor and closer to the path is more confident
    return DetectorVote(
        request_id=request.request_id,
        detector_id=DETECTOR_ID,
        verdict="normal",
        confidence=_confidence(1 - dist / limit),
        reason=f"{dist:.0f} m from planned route (limit {limit:.0f} m)",
    )
