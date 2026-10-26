from detectors.common import abstain_reason, confidence, usable_checkins
from detectors.geo import haversine_m
from detectors.models import DetectorVote

DETECTOR_ID = "dwell-time"
# step modes are walk bus train
TRANSIT_MODES = {"bus", "train"}
# stationary window this share of the limit counts as approaching it
APPROACH_FRACTION = 0.25


def _abstain(request, reason):
    return DetectorVote.abstain(request.request_id, DETECTOR_ID, reason)


# newest check-ins that all sit within radius of their centre
def _stationary_run(usable, radius_m):
    for start in range(len(usable)):
        run = usable[start:]
        lat = sum(c.lat for c in run) / len(run)
        lng = sum(c.lng for c in run) / len(run)
        if all(haversine_m(lat, lng, c.lat, c.lng) <= radius_m for c in run):
            return run
    # nothing stationary so just the newest one
    return usable[-1:]


# longer limit when waiting for a bus or train
def _limit_s(ctx, settings):
    steps = ctx.steps or []
    mode = None
    if ctx.step_index is not None and 0 <= ctx.step_index < len(steps):
        mode = steps[ctx.step_index].get("mode")
    return settings.dwell_transit_s if mode in TRANSIT_MODES else settings.dwell_walk_s


def detect(request, ctx, settings):
    reason = abstain_reason(request, ctx, settings)
    if reason:
        return _abstain(request, reason)

    usable = usable_checkins(ctx.recent, settings)
    # how long they have stayed in one spot by timestamps
    run = _stationary_run(usable, settings.dwell_radius_m)
    duration = (run[-1].timestamp - run[0].timestamp).total_seconds()
    limit = _limit_s(ctx, settings)

    # over the limit is a long dwell
    if duration > limit:
        score = 0.5 + 0.5 * min(1.0, (duration - limit) / limit)
        return DetectorVote(
            request_id=request.request_id,
            detector_id=DETECTOR_ID,
            verdict="problem",
            problem_type="long_dwell",
            confidence=confidence(score),
            reason=f"stationary for {duration:.0f} s within {settings.dwell_radius_m:g} m (limit {limit} s)",
        )

    # whole window stationary and near the limit so real dwell may be longer
    if len(run) == len(usable) and len(run) > 1 and duration >= APPROACH_FRACTION * limit:
        return _abstain(
            request,
            f"window too short to judge: stationary for the whole {duration:.0f} s window (limit {limit} s)",
        )

    return DetectorVote(
        request_id=request.request_id,
        detector_id=DETECTOR_ID,
        verdict="normal",
        confidence=confidence(1 - duration / limit),
        reason=f"stationary for {duration:.0f} s (limit {limit} s)",
    )
