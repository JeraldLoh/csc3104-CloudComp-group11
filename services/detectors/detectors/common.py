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
