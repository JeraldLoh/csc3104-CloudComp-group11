from detectors.models import DetectorVote

# step modes that ride a vehicle
TRANSIT_MODES = {"bus", "train"}


# a cannot decide vote from this detector
def abstain(request, detector_id, reason):
    return DetectorVote.abstain(request.request_id, detector_id, reason)


# a normal or problem vote from this detector
def vote(request, detector_id, verdict, conf, reason, problem=None):
    return DetectorVote(
        request_id=request.request_id,
        detector_id=detector_id,
        verdict=verdict,
        problem_type=problem,
        confidence=conf,
        reason=reason
    )


# step dict or None when the index is unusable
def step_at(steps, index):
    if index is None or not steps or not 0 <= index < len(steps) or not isinstance(steps[index], dict):
        return None
    return steps[index]


# mode of the current step or None
def step_mode(ctx):
    step = step_at(ctx.steps, ctx.step_index)
    return step.get("mode") if step else None


# why a detector must not judge this request or None if it can
def abstain_reason(request, ctx, settings):
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


# check-ins with trustworthy gps in the same order
def usable_checkins(checkins, settings):
    return [
        c for c in checkins if c.accuracy_m is not None and c.accuracy_m <= settings.gps_max_accuracy_m
    ]


# maps a 0 to 1 score onto 0.5 to 0.95
def confidence(score):
    return round(0.5 + 0.45 * min(1.0, max(0.0, score)), 2)
