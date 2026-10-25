"""Dwell-time detector: has the traveller been standing in one place too long?

Pure function, no I/O. The stationary run is the newest check-ins that all lie
within ``DWELL_RADIUS_M`` of their centroid; its duration comes from the
timestamps, not the count. The limit depends on the current step's mode:
``DWELL_TRANSIT_S`` for bus/mrt (waiting for a vehicle), otherwise
``DWELL_WALK_S``. Over the limit is ``problem`` / ``long_dwell``.

Assumption: ``trip:{id}:recent`` holds only 10 check-ins (~100 s), shorter than
either limit. So a dwell can only be *proven* over the limit if the window
itself spans it. When the whole window is stationary the true dwell is at
least the window span, but we can't tell how much longer. If that span is
already ``APPROACH_FRACTION`` of the limit we abstain ("window too short")
instead of guessing normal. Asking M2 to store a ``dwellStartAt`` in
``trip:{id}:context`` would remove this blind spot.
"""

from typing import List

from detectors.common import abstain_reason, confidence, usable_checkins
from detectors.config import Settings
from detectors.context import TripContext
from detectors.geo import haversine_m
from detectors.models import CheckIn, DetectorRequest, DetectorVote

DETECTOR_ID = "dwell-time"
TRANSIT_MODES = {"bus", "mrt"}
# A fully stationary window spanning this share of the limit is "approaching" it
APPROACH_FRACTION = 0.25


def _abstain(request: DetectorRequest, reason: str) -> DetectorVote:
    return DetectorVote.abstain(request.request_id, DETECTOR_ID, reason)


def _stationary_run(usable: List[CheckIn], radius_m: float) -> List[CheckIn]:
    """The longest newest-ending run whose points are all within ``radius_m`` of its centroid."""
    for start in range(len(usable)):
        run = usable[start:]
        lat = sum(c.lat for c in run) / len(run)
        lng = sum(c.lng for c in run) / len(run)
        if all(haversine_m(lat, lng, c.lat, c.lng) <= radius_m for c in run):
            return run
    return usable[-1:]


def _limit_s(ctx: TripContext, settings: Settings) -> int:
    steps = ctx.steps or []
    mode = None
    if ctx.step_index is not None and 0 <= ctx.step_index < len(steps):
        mode = steps[ctx.step_index].get("mode")
    return settings.dwell_transit_s if mode in TRANSIT_MODES else settings.dwell_walk_s


def detect(request: DetectorRequest, ctx: TripContext, settings: Settings) -> DetectorVote:
    reason = abstain_reason(request, ctx, settings)
    if reason:
        return _abstain(request, reason)

    usable = usable_checkins(ctx.recent, settings)
    run = _stationary_run(usable, settings.dwell_radius_m)
    duration = (run[-1].timestamp - run[0].timestamp).total_seconds()
    limit = _limit_s(ctx, settings)

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

    # Whole window stationary and nearing the limit: the real dwell may be longer
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
