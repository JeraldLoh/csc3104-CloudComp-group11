import math
import statistics
from datetime import timedelta

from detectors.common import abstain_reason, confidence, usable_checkins
from detectors.geo import angle_diff_deg, bearing_deg, haversine_m, nearest_segment
from detectors.models import DetectorVote

DETECTOR_ID = "heading-speed"
# moving check-ins needed to judge direction
MIN_MOVING_POINTS = 3
# closer than this the bearing is mostly gps noise
MIN_BEARING_DISTANCE_M = 2.0
# without speed compare against a fix at least this far back
BASELINE_S = 30
# fixes must be further apart than this times their combined accuracy
NOISE_FACTOR = 1.5


def _abstain(request, reason):
    return DetectorVote.abstain(request.request_id, DETECTOR_ID, reason)


# bearing from an older fix when the check-in has no speed
def _baseline_bearing(earlier, c, settings):
    cutoff = c.timestamp - timedelta(seconds=BASELINE_S)
    # newest fix that is at least BASELINE_S older
    ref = next((p for p in reversed(earlier) if p.timestamp <= cutoff), None)
    if ref is None:
        return None
    dist = haversine_m(ref.lat, ref.lng, c.lat, c.lng)
    elapsed = (c.timestamp - ref.timestamp).total_seconds()
    # too close to tell apart from gps jitter
    if dist <= NOISE_FACTOR * math.hypot(ref.accuracy_m, c.accuracy_m):
        return None
    # too slow to count as moving
    if dist / elapsed < settings.min_moving_speed_mps:
        return None
    return bearing_deg(ref.lat, ref.lng, c.lat, c.lng)


# bearing of travel for each check-in that is actually moving
def _moving_bearings(usable, settings):
    moving = []
    for i, c in enumerate(usable):
        prev = usable[i - 1] if i else None
        # no speed so judge movement over a longer gap
        if c.speed_mps is None:
            bearing = _baseline_bearing(usable[:i], c, settings)
        # moving so use the step from the previous fix or the reported heading
        elif c.speed_mps >= settings.min_moving_speed_mps:
            bearing = None
            if prev is not None and haversine_m(prev.lat, prev.lng, c.lat, c.lng) >= MIN_BEARING_DISTANCE_M:
                bearing = bearing_deg(prev.lat, prev.lng, c.lat, c.lng)
            elif c.heading_deg is not None:
                bearing = c.heading_deg
        else:
            bearing = None
        if bearing is not None:
            moving.append((bearing, c))
    return moving


# extra reason text when walking speed looks too high
def _speed_note(request, ctx, settings):
    speed = request.checkin.speed_mps
    steps = ctx.steps or []
    if speed is None or ctx.step_index is None or not 0 <= ctx.step_index < len(steps):
        return ""
    if steps[ctx.step_index].get("mode") == "walk" and speed > settings.walk_max_speed_mps:
        return f"; speed {speed:.1f} m/s is high for walking"
    return ""


def detect(request, ctx, settings):
    reason = abstain_reason(request, ctx, settings)
    if reason:
        return _abstain(request, reason)

    # standing still says nothing about direction so abstain
    usable = usable_checkins(ctx.recent, settings)
    moving = _moving_bearings(usable, settings)
    if len(moving) < MIN_MOVING_POINTS:
        return _abstain(
            request,
            f"not moving enough to judge direction ({len(moving)} of {len(usable)} "
            f"check-ins moving, need {MIN_MOVING_POINTS})",
        )

    # angle between travel and the nearest path segment
    diffs = []
    for bearing, c in moving:
        _, _, path_bearing = nearest_segment((c.lat, c.lng), ctx.path)
        diffs.append(angle_diff_deg(bearing, path_bearing))
    # median so a few noisy bearings do not decide
    median = statistics.median(diffs)
    note = _speed_note(request, ctx, settings)
    limit = settings.wrong_dir_deg

    if median > limit:
        # further past the limit and more moving check-ins raise confidence
        score = 0.5 * (median - limit) / max(180 - limit, 1e-9) + 0.5 * min(1.0, len(moving) / 6)
        return DetectorVote(
            request_id=request.request_id,
            detector_id=DETECTOR_ID,
            verdict="problem",
            problem_type="wrong_direction",
            confidence=confidence(score),
            reason=(
                f"moving {median:.0f}° off the route direction (limit {limit:g}°) "
                f"over {len(moving)} moving check-ins{note}"
            ),
        )

    return DetectorVote(
        request_id=request.request_id,
        detector_id=DETECTOR_ID,
        verdict="normal",
        confidence=confidence(1 - median / limit),
        reason=f"moving {median:.0f}° off the route direction (limit {limit:g}°){note}",
    )
