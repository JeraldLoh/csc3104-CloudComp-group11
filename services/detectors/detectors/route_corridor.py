from detectors.common import abstain, abstain_reason, confidence, usable_checkins, vote
from detectors.geo import nearest_segment

DETECTOR_ID = "route-corridor"



# off route when the newest check-ins in a row are all outside the corridor
def detect(request, ctx, settings):
    reason = abstain_reason(request, ctx, settings)
    if reason:
        return abstain(request, DETECTOR_ID, reason)

    # check-ins in a row needed before calling it off route
    needed = settings.corridor_consecutive
    # newest first and includes this check-in
    usable = list(reversed(usable_checkins(ctx.recent, settings)))
    if len(usable) < needed:
        return abstain(request, DETECTOR_ID, f"only {len(usable)} usable check-in(s), need {needed}")

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
        return vote(
            request, DETECTOR_ID, "problem", confidence(score),
            f"{dist:.0f} m from planned route (limit {limit:.0f} m) for {run} check-ins",
            "off_route"
        )

    # newest is outside but not enough in a row yet
    if dist > limit:
        outside = sum(1 for d, lim in measures[:needed] if d > lim)
        return vote(
            request, DETECTOR_ID, "normal", confidence(0),
            f"{dist:.0f} m from planned route (limit {limit:.0f} m), "
            f"but only {outside} of the last {needed} check-ins are outside"
        )

    # inside the corridor and closer to the path is more confident
    return vote(
        request, DETECTOR_ID, "normal", confidence(1 - dist / limit),
        f"{dist:.0f} m from planned route (limit {limit:.0f} m)"
    )
