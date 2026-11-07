import bisect

from detectors.geo import bearing_deg, cumulative_m, project_onto_path
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


# lat lng of a route place or None
def place_point(place):
    try:
        return float(place["lat"]), float(place["lng"])
    except (TypeError, KeyError, ValueError):
        return None


# start and end along the path per step and end is None without a stop
def step_ranges(steps, path, cum):
    ranges = []
    start = 0.0
    for step in steps:
        to = place_point(step.get("to")) if isinstance(step, dict) else None
        end = project_onto_path(to, path, cum, lo_m=start)[0] if to else None
        ranges.append((start, end))
        if end is not None:
            start = end
    return ranges


# one step part of the path so a route passing near itself is not mistaken for this step
class StepSection:
    # whole path when the step is unknown
    def __init__(self, ctx, step_index):
        self._path = ctx.path
        self._cum = cumulative_m(ctx.path)
        self._lo = self._hi = None
        if step_at(ctx.steps, step_index) is not None:
            self._lo, self._hi = step_ranges(ctx.steps, ctx.path, self._cum)[step_index]

    # section for a check-in own step or the trip step when the check-in has none
    @classmethod
    def per_checkin(cls, ctx):
        sections = {}

        def section(c):
            index = c.step_index if step_at(ctx.steps, c.step_index) is not None else ctx.step_index
            if index not in sections:
                sections[index] = cls(ctx, index)
            return sections[index]
        return section

    def _project(self, c):
        return project_onto_path((c.lat, c.lng), self._path, self._cum, lo_m=self._lo, hi_m=self._hi)

    # metres from a check-in to this part of the path
    def away_m(self, c):
        return self._project(c)[1]

    # direction of this part of the path at the point closest to a check-in
    def bearing_deg(self, c):
        along = self._project(c)[0]
        i = bisect.bisect_right(self._cum, along) - 1
        # at the step end take the segment arriving there not the one leaving
        if self._hi is not None and self._cum[i] >= self._hi:
            i -= 1
        i = min(max(i, 0), len(self._path) - 2)
        a, b = self._path[i], self._path[i + 1]
        return bearing_deg(a[0], a[1], b[0], b[1])


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
