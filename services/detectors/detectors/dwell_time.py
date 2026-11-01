from datetime import datetime

from detectors.common import TRANSIT_MODES, abstain, abstain_reason, confidence, step_mode, usable_checkins, vote
from detectors.context import _text
from detectors.geo import haversine_m

DETECTOR_ID = "dwell-time"
# stationary window this share of the limit counts as approaching it
APPROACH_FRACTION = 0.25
# journey keys expire a day after their last update
KEY_TTL_S = 86400


# centre of a run of check-ins
def _centre(run):
    return sum(c.lat for c in run) / len(run), sum(c.lng for c in run) / len(run)


# where the traveller stopped and since when, kept in redis because the recent list only spans 90 s
class DwellStart:
    def __init__(self, redis, trip_id):
        self._redis = redis
        self._key = f"trip:{trip_id}:dwell"

    # start of the current stop and whether it came from redis rather than the window
    def find(self, run, radius_m):
        newest = run[-1]
        saved = self._load()
        # newest still at the saved spot so the stop began then
        if saved and haversine_m(saved[0], saved[1], newest.lat, newest.lng) <= radius_m and saved[2] <= newest.timestamp:
            return saved[2], True
        self._save(*_centre(run), run[0].timestamp)
        return run[0].timestamp, False

    # saved lat lng and start or None when missing or malformed
    def _load(self):
        if self._redis is None:
            return None
        raw = {_text(k): _text(v) for k, v in self._redis.hgetall(self._key).items()}
        try:
            return float(raw["lat"]), float(raw["lng"]), datetime.fromisoformat(raw["since"])
        except (KeyError, ValueError):
            return None

    def _save(self, lat, lng, since):
        if self._redis is None:
            return
        pipe = self._redis.pipeline()
        pipe.hset(self._key, mapping={"lat": lat, "lng": lng, "since": since.isoformat()})
        pipe.expire(self._key, KEY_TTL_S)
        pipe.execute()



# newest check-ins that all sit within radius of their centre
def _stationary_run(usable, radius_m):
    for start in range(len(usable)):
        run = usable[start:]
        lat, lng = _centre(run)
        if all(haversine_m(lat, lng, c.lat, c.lng) <= radius_m for c in run):
            return run
    # nothing stationary so just the newest one
    return usable[-1:]


# longer limit when waiting for a bus or train
def _limit_s(ctx, settings):
    return settings.dwell_transit_s if step_mode(ctx) in TRANSIT_MODES else settings.dwell_walk_s


# long dwell when stationary past the step limit
def detect(request, ctx, settings):
    reason = abstain_reason(request, ctx, settings)
    if reason:
        return abstain(request, DETECTOR_ID, reason)

    usable = usable_checkins(ctx.recent, settings)
    # how long they have stayed in one spot by timestamps
    run = _stationary_run(usable, settings.dwell_radius_m)
    since, saved = DwellStart(ctx.redis, request.checkin.trip_id).find(run, settings.dwell_radius_m)
    duration = (run[-1].timestamp - since).total_seconds()
    limit = _limit_s(ctx, settings)

    # over the limit is a long dwell
    if duration > limit:
        score = 0.5 + 0.5 * min(1.0, (duration - limit) / limit)
        return vote(
            request, DETECTOR_ID, "problem", confidence(score),
            f"stationary for {duration:.0f} s within {settings.dwell_radius_m:g} m (limit {limit} s)",
            "long_dwell"
        )

    # whole window stationary and near the limit so real dwell may be longer unless redis knows the start
    if not saved and len(run) == len(usable) and len(run) > 1 and duration >= APPROACH_FRACTION * limit:
        return abstain(
            request, DETECTOR_ID,
            f"window too short to judge: stationary for the whole {duration:.0f} s window (limit {limit} s)"
        )

    return vote(
        request, DETECTOR_ID, "normal", confidence(1 - duration / limit),
        f"stationary for {duration:.0f} s (limit {limit} s)"
    )
