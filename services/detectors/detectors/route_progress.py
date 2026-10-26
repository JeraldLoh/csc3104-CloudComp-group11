from dataclasses import dataclass

from detectors.common import abstain_reason, confidence, usable_checkins
from detectors.geo import cumulative_m, haversine_m, project_onto_path
from detectors.models import CheckIn, DetectorVote

DETECTOR_ID = "route-progress"
TRANSIT_MODES = {"bus", "train"}


# one check-in placed on the path
@dataclass(frozen=True)
class _Fix:
    checkin: CheckIn
    along_m: float  # distance along the path
    away_m: float  # distance from the path
    limit_m: float  # corridor plus gps accuracy

    @property
    def on_path(self):
        return self.away_m <= self.limit_m

    @property
    def accuracy_m(self):
        return self.checkin.accuracy_m or 0.0


def _abstain(request, reason):
    return DetectorVote.abstain(request.request_id, DETECTOR_ID, reason)


def _vote(request, verdict, conf, reason, problem=None):
    return DetectorVote(
        request_id=request.request_id,
        detector_id=DETECTOR_ID,
        verdict=verdict,
        problem_type=problem,
        confidence=conf,
        reason=reason,
    )


# lat lng of a stop or None
def _point(place):
    try:
        return float(place["lat"]), float(place["lng"])
    except (TypeError, KeyError, ValueError):
        return None


def _step_ranges(steps, path, cum):
    # start and end along the path per step and end is None without a stop
    ranges = []
    start = 0.0
    for step in steps:
        to = _point(step.get("to")) if isinstance(step, dict) else None
        end = project_onto_path(to, path, cum, lo_m=start)[0] if to else None
        ranges.append((start, end))
        if end is not None:
            start = end
    return ranges


# step dict or None when the index is unusable
def _step(steps, index):
    if index is None or not steps or not 0 <= index < len(steps) or not isinstance(steps[index], dict):
        return None
    return steps[index]


def _locate(usable, ctx, cum, ranges, settings):
    # match within own step and reach of previous fix so loops are not mistaken for progress
    fixes = []
    for c in usable:
        index = c.step_index if _step(ctx.steps, c.step_index) is not None else ctx.step_index
        step = _step(ctx.steps, index)
        # vehicles may move fast and walking steps may not
        transit = step is not None and step.get("mode") in TRANSIT_MODES
        speed = settings.walk_max_speed_mps if step is not None and not transit else settings.progress_max_speed_mps
        acc = c.accuracy_m or 0.0
        # search range along the path
        lo, hi = -float("inf"), float("inf")
        if step is not None and ranges[index][1] is not None:
            start, end = ranges[index]
            slack = acc + settings.progress_min_m
            lo = start - slack
            if not transit:  # a missed stop rides past the end
                hi = end + slack
        # stay within reach of the previous fix
        if fixes:
            prev = fixes[-1]
            dt = max(0.0, (c.timestamp - prev.checkin.timestamp).total_seconds())
            reach = speed * dt + acc + prev.accuracy_m + settings.progress_min_m
            lo, hi = max(lo, prev.along_m - reach), min(hi, prev.along_m + reach)
            if lo > hi:  # step changed beyond reach so trust the step
                lo, hi = min(lo, hi), max(lo, hi)
        along, away = project_onto_path((c.lat, c.lng), ctx.path, cum, lo_m=lo, hi_m=hi)
        fixes.append(_Fix(c, along, away, settings.corridor_m + acc))
    return fixes


def _distance(a, b):
    return haversine_m(a.lat, a.lng, b[0], b[1])


def _clear_margin(a, b, settings):
    # smaller changes could be gps noise
    return a.accuracy_m + b.accuracy_m + settings.progress_min_m


# still on the vehicle well past the stop
def _missed_stop(request, fixes, step, end_m, settings):
    if step is None or end_m is None or step.get("mode") not in TRANSIT_MODES:
        return None
    stop = _point(step.get("to"))
    if stop is None:
        return None
    run = fixes[-settings.corridor_consecutive:]
    margin = settings.missed_stop_m

    # beyond the stop along the route and far from it
    def past(f):
        return f.along_m >= end_m - f.limit_m and _distance(f.checkin, stop) > margin + f.accuracy_m

    if not all(past(f) for f in run):
        return None
    first, last = run[0].checkin, run[-1].checkin
    dt = (last.timestamp - first.timestamp).total_seconds()
    d_first, d_last = _distance(first, stop), _distance(last, stop)
    if dt <= 0 or d_last <= d_first:
        return None  # not moving away from the stop
    # straight line speed over the run
    speed = haversine_m(first.lat, first.lng, last.lat, last.lng) / dt
    name = step["to"].get("name") or "the stop"
    if speed <= settings.walk_max_speed_mps:
        # walking on may mean they got off and the step has not advanced yet
        return _vote(
            request, "normal", settings.progress_unsure_confidence,
            f"{d_last:.0f} m past {name} at walking speed {speed:.1f} m/s, may have got off",
        )
    # further past and faster raise confidence
    score = 0.5 * min(1.0, (d_last - margin) / margin) + 0.5 * min(1.0, speed / (2 * settings.walk_max_speed_mps))
    return _vote(
        request, "problem", confidence(score),
        f"{d_last:.0f} m past {name} and moving away at {speed:.1f} m/s, still on step {step.get('stepIndex')}",
        "missed_stop",
    )


# on the path but clearly behind the furthest point reached
def _wrong_direction(request, fixes, settings):
    run = fixes[-settings.corridor_consecutive:]
    if not all(f.on_path for f in run):
        return None
    # furthest point reached in the window
    peak = max((f for f in fixes if f.on_path), key=lambda f: f.along_m)
    # still at the furthest point so not going back
    if any(f is peak for f in run):
        return None
    if not all(peak.along_m - f.along_m > _clear_margin(peak, f, settings) for f in run):
        return None
    last = run[-1]
    back = peak.along_m - last.along_m
    margin = _clear_margin(peak, last, settings)
    return _vote(
        request, "problem", confidence((back - margin) / margin),
        f"{back:.0f} m back along the route from the furthest point reached",
        "wrong_direction",
    )


# distance left to the end along the route
def _remaining(f, length):
    return length - f.along_m + f.away_m


# progress toward the end measured along the route
def detect(request, ctx, settings):
    reason = abstain_reason(request, ctx, settings)
    if reason:
        return _abstain(request, reason)

    needed = settings.corridor_consecutive
    usable = usable_checkins(ctx.recent, settings)
    if len(usable) < needed:
        return _abstain(request, f"only {len(usable)} usable check-in(s), need {needed}")

    # path length so far at each point
    cum = cumulative_m(ctx.path)
    length = cum[-1]
    # where each step starts and ends on the path
    ranges = _step_ranges(ctx.steps or [], ctx.path, cum)
    step = _step(ctx.steps, ctx.step_index)
    end_m = ranges[ctx.step_index][1] if step is not None else None
    fixes = _locate(usable, ctx, cum, ranges, settings)

    # first match wins in order missed stop then wrong direction then off route
    vote = _missed_stop(request, fixes, step, end_m, settings) or _wrong_direction(request, fixes, settings)
    if vote:
        return vote

    first, last = fixes[0], fixes[-1]
    left = _remaining(last, length)
    gain = _remaining(first, length) - left  # metres closer over the window
    margin = _clear_margin(first, last, settings)
    unsure = settings.progress_unsure_confidence

    # off the path for long enough
    if all(not f.on_path for f in fixes[-needed:]):
        if gain > margin:
            # detour still heading to the end so low confidence to not outvote route corridor
            return _vote(
                request, "normal", unsure,
                f"{last.away_m:.0f} m off the route but {gain:.0f} m closer to the end, detour?",
            )
        # further off and moving away raise confidence
        excess = (last.away_m - last.limit_m) / last.limit_m
        away = max(0.0, -gain) / last.limit_m
        return _vote(
            request, "problem", confidence(0.5 * min(1.0, excess) + 0.5 * min(1.0, away)),
            f"{last.away_m:.0f} m off the route, limit {last.limit_m:.0f} m, "
            f"{'not getting closer' if gain >= 0 else f'{-gain:.0f} m further'} over {len(fixes)} check-ins",
            "off_route",
        )
    # only just left the path so not sure yet
    if not last.on_path:
        return _vote(request, "normal", unsure, f"{last.away_m:.0f} m off the route, just left it")

    # arrived
    if length - last.along_m <= last.limit_m:
        return _vote(request, "normal", confidence(1), "at the end of the route")
    # clear progress on the path
    if gain > margin:
        return _vote(
            request, "normal", confidence(gain / (2 * margin)),
            f"{gain:.0f} m closer to the end over {len(fixes)} check-ins, {left:.0f} m left",
        )
    return _vote(request, "normal", confidence(0), f"on the route, no clear progress, {left:.0f} m left")
