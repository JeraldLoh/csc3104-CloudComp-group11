"""Heading/speed detector: is the traveller moving against the route direction?

Pure function, no I/O. For each recent check-in that is actually moving, it
takes the bearing travelled since the previous usable check-in and compares it
with the direction of the nearest path segment. The vote is ``problem`` /
``wrong_direction`` when the *median* difference across the window is above
``WRONG_DIR_DEG``; the median keeps a few noisy GPS bearings from deciding it.

Check-ins come from a web page, which often reports no speed. Those are judged by how
far the traveller moved over the last 30 s or more, compared with the GPS accuracy.

Standing still says nothing about direction, so with fewer than
``MIN_MOVING_POINTS`` moving check-ins it abstains rather than guessing normal.
"""

import math
import statistics
from datetime import timedelta
from typing import List, Optional, Tuple

from detectors.common import abstain_reason, confidence, usable_checkins
from detectors.config import Settings
from detectors.context import TripContext
from detectors.geo import angle_diff_deg, bearing_deg, haversine_m, nearest_segment
from detectors.models import CheckIn, DetectorRequest, DetectorVote

DETECTOR_ID = "heading-speed"
MIN_MOVING_POINTS = 3
# Closer than this, the bearing between two fixes is mostly GPS noise
MIN_BEARING_DISTANCE_M = 2.0
# Web-app check-ins often have no speed. Then movement is judged against an earlier
# fix at least this far back in time: real movement grows with the gap, GPS jitter doesn't.
BASELINE_S = 30
# ...and the fixes must be further apart than this many times their combined accuracy
NOISE_FACTOR = 1.5


def _abstain(request: DetectorRequest, reason: str) -> DetectorVote:
    return DetectorVote.abstain(request.request_id, DETECTOR_ID, reason)


def _baseline_bearing(earlier: List[CheckIn], c: CheckIn, settings: Settings) -> Optional[float]:
    """Bearing travelled over the last ``BASELINE_S`` or more, or None if not clearly moving.

    Used when the browser reports no speed. None also covers "no fix far enough back
    yet", so an unclear case never counts as moving.
    """
    cutoff = c.timestamp - timedelta(seconds=BASELINE_S)
    ref = next((p for p in reversed(earlier) if p.timestamp <= cutoff), None)
    if ref is None:
        return None
    dist = haversine_m(ref.lat, ref.lng, c.lat, c.lng)
    elapsed = (c.timestamp - ref.timestamp).total_seconds()
    if dist <= NOISE_FACTOR * math.hypot(ref.accuracy_m, c.accuracy_m):
        return None
    if dist / elapsed < settings.min_moving_speed_mps:
        return None
    return bearing_deg(ref.lat, ref.lng, c.lat, c.lng)


def _moving_bearings(usable: List[CheckIn], settings: Settings) -> List[Tuple[float, CheckIn]]:
    """(movement bearing, check-in) for each moving check-in, oldest first.

    With a reported speed, the bearing is measured from the previous usable
    check-in; it falls back to the reported ``headingDeg`` when there is no
    previous check-in in the window or the two fixes are too close together to
    give a stable bearing. Without a reported speed, see :func:`_baseline_bearing`.
    """
    moving = []
    for i, c in enumerate(usable):
        prev = usable[i - 1] if i else None
        if c.speed_mps is None:
            bearing = _baseline_bearing(usable[:i], c, settings)
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


def _speed_note(request: DetectorRequest, ctx: TripContext, settings: Settings) -> str:
    """Extra text for the reason when the speed looks wrong for the step's mode."""
    speed = request.checkin.speed_mps
    steps = ctx.steps or []
    if speed is None or ctx.step_index is None or not 0 <= ctx.step_index < len(steps):
        return ""
    if steps[ctx.step_index].get("mode") == "walk" and speed > settings.walk_max_speed_mps:
        return f"; speed {speed:.1f} m/s is high for walking"
    return ""


def detect(request: DetectorRequest, ctx: TripContext, settings: Settings) -> DetectorVote:
    reason = abstain_reason(request, ctx, settings)
    if reason:
        return _abstain(request, reason)

    usable = usable_checkins(ctx.recent, settings)
    moving = _moving_bearings(usable, settings)
    if len(moving) < MIN_MOVING_POINTS:
        return _abstain(
            request,
            f"not moving enough to judge direction ({len(moving)} of {len(usable)} "
            f"check-ins moving, need {MIN_MOVING_POINTS})",
        )

    diffs = []
    for bearing, c in moving:
        _, _, path_bearing = nearest_segment((c.lat, c.lng), ctx.path)
        diffs.append(angle_diff_deg(bearing, path_bearing))
    median = statistics.median(diffs)
    note = _speed_note(request, ctx, settings)
    limit = settings.wrong_dir_deg

    if median > limit:
        # Further past the limit and more moving check-ins both raise confidence
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
