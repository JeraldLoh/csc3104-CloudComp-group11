import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from detectors.config import Settings
from detectors.context import TripContext
from detectors.models import CheckIn, DetectorRequest
from detectors.geo import cumulative_m
from detectors.route_progress import _locate, _step_ranges, detect

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = Settings()  # corridor 100 m, 3 consecutive, progress 20 m, missed stop 150 m, unsure 0.3
T0 = datetime(2026, 10, 12, 8, 0, 0, tzinfo=timezone.utc)
M_LAT = 1 / 111_195  # one metre north, in degrees
M_LNG = 1 / 111_160  # one metre east at ~1.4 N, in degrees
LAT0, LNG0 = 1.40, 103.9


def xy(north_m, east_m):
    """(lat, lng) of a point ``north_m``/``east_m`` metres from (LAT0, LNG0)."""
    return LAT0 + north_m * M_LAT, LNG0 + east_m * M_LNG


# 2 km straight north
PATH = [xy(0, 0), xy(2000, 0)]
WALK = [{"stepIndex": 0, "mode": "walk", "to": {"name": "End", "lat": PATH[-1][0], "lng": PATH[-1][1]}}]


def checkin(seq, north_m, east_m=0.0, every=10, accuracy=10.0, step=0):
    lat, lng = xy(north_m, east_m)
    return CheckIn.model_validate(
        {
            "tripId": "T-001",
            "travellerId": "U-042",
            "seq": seq,
            "timestamp": (T0 + timedelta(seconds=every * seq)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "lat": lat,
            "lng": lng,
            "accuracyM": accuracy,
            "speedMps": None,
            "headingDeg": None,
            "stepIndex": step,
            "routeVersion": 3,
            "status": "active",
        }
    )


def track(points, every=10, step=0, accuracy=10.0):
    """Check-ins at ``[(north_m, east_m), ...]``, ``every`` seconds apart."""
    return [checkin(i, n, e, every=every, step=step, accuracy=accuracy) for i, (n, e) in enumerate(points)]


def vote_for(recent, path=PATH, steps=WALK, step_index=0, **ctx_kwargs):
    ctx = TripContext(path=path, steps=steps, step_index=step_index, route_version=3, recent=recent, **ctx_kwargs)
    request = DetectorRequest(request_id="T-001#x", risk_level="normal", checkin=recent[-1])
    return detect(request, ctx, SETTINGS)


# --- normal ---------------------------------------------------------------------------


def test_walking_along_the_route_is_confident_normal():
    vote = vote_for(track([(100 + 13 * i, 5) for i in range(10)]))
    assert vote.verdict == "normal" and vote.problem_type is None
    assert vote.detector_id == "route-progress"
    assert vote.confidence > 0.8
    assert "closer to the end" in vote.reason


def test_standing_on_the_route_is_plain_normal():
    vote = vote_for(track([(500, 3)] * 10))
    assert vote.verdict == "normal" and vote.confidence == 0.5
    assert "no clear progress" in vote.reason


def test_parallel_street_detour_is_unsure_normal():
    # 150 m east of the route, still walking north towards the end
    vote = vote_for(track([(300 + 13 * i, 150) for i in range(10)]))
    assert vote.verdict == "normal"
    assert vote.confidence == SETTINGS.progress_unsure_confidence == 0.3
    assert "detour" in vote.reason


def test_just_left_the_route_is_unsure_normal():
    vote = vote_for(track([(300, 0)] * 8 + [(300, 160)] * 2))
    assert vote.verdict == "normal" and vote.confidence == 0.3
    assert "just left" in vote.reason


def test_at_the_end_of_the_route_is_normal():
    vote = vote_for(track([(1990, 0)] * 5))
    assert vote.verdict == "normal" and vote.reason == "at the end of the route"


def test_route_that_bends_away_from_the_destination_is_still_progress():
    # East 500 m, north 500 m, then back west 500 m: the end is only 500 m north of the start.
    # Walking the first leg takes the traveller further from the end in a straight line.
    path = [xy(0, 0), xy(0, 500), xy(500, 500), xy(500, 0)]
    steps = [{"stepIndex": 0, "mode": "walk", "to": {"name": "End", "lat": path[-1][0], "lng": path[-1][1]}}]
    vote = vote_for(track([(0, 50 + 13 * i) for i in range(10)]), path=path, steps=steps)
    assert vote.verdict == "normal" and vote.confidence > 0.8


# --- off_route ------------------------------------------------------------------------


def test_walking_away_from_the_route_is_off_route():
    vote = vote_for(track([(300, 13 * i) for i in range(20)])[-10:])
    assert vote.verdict == "problem" and vote.problem_type == "off_route"
    assert 0.5 <= vote.confidence <= 0.95
    assert "further" in vote.reason


def test_standing_off_the_route_is_off_route():
    vote = vote_for(track([(300, 200)] * 10))
    assert vote.verdict == "problem" and vote.problem_type == "off_route"
    assert "not getting closer" in vote.reason


def test_off_route_limit_includes_gps_accuracy():
    # 140 m is outside 100 m but inside 100 + 45 m
    assert vote_for(track([(300, 140)] * 10, accuracy=45)).verdict == "normal"
    assert vote_for(track([(300, 140)] * 10, accuracy=10)).problem_type == "off_route"


# --- wrong_direction ------------------------------------------------------------------


def test_walking_back_along_the_route_is_wrong_direction():
    points = [(300 + 13 * i, 0) for i in range(5)] + [(352 - 13 * i, 0) for i in range(1, 8)]
    vote = vote_for(track(points)[-10:])
    assert vote.verdict == "problem" and vote.problem_type == "wrong_direction"
    assert "back along the route" in vote.reason


def test_small_wobble_backwards_is_not_wrong_direction():
    # 30 m back is within GPS noise (10 + 10 + 20 m)
    points = [(300 + 13 * i, 0) for i in range(7)] + [(378 - 10 * i, 0) for i in range(1, 4)]
    assert vote_for(track(points)).verdict == "normal"


def test_out_and_back_route_is_matched_to_the_right_leg():
    # Walk 500 m north, then come back south on the same street to an end beside the start.
    # Step 1 (the way back) must not be read as walking backwards along step 0.
    path = [xy(0, 0), xy(500, 0), xy(0, 40)]
    steps = [
        {"stepIndex": 0, "mode": "walk", "to": {"name": "Turn", "lat": path[1][0], "lng": path[1][1]}},
        {"stepIndex": 1, "mode": "walk", "to": {"name": "End", "lat": path[2][0], "lng": path[2][1]}},
    ]
    # 3 m east: nearer the way out (0 m east) than the way back (~8 m east here)
    back = track([(400 - 13 * i, 3) for i in range(10)], step=1)
    vote = vote_for(back, path=path, steps=steps, step_index=1)
    assert vote.verdict == "normal" and vote.confidence > 0.8
    # Without the step, the same check-ins look like walking back along the way out
    assert vote_for(back, path=path, steps=None, step_index=None).problem_type == "wrong_direction"


def test_off_route_traveller_is_not_matched_to_a_later_leg_nearby():
    # A U-shaped route: step 0 walks north on x=0, the bus (step 1) comes back south on x=300.
    # Drifting east from step 0 brings the traveller near the bus leg; that is not progress.
    path = [xy(0, 0), xy(1000, 0), xy(1000, 300), xy(0, 300)]
    steps = [
        {"stepIndex": 0, "mode": "walk", "to": {"name": "Stop", "lat": path[1][0], "lng": path[1][1]}},
        {"stepIndex": 1, "mode": "bus", "to": {"name": "End", "lat": path[3][0], "lng": path[3][1]}},
    ]
    drift = track([(300, 13 * i) for i in range(16, 26)])  # 208 -> 325 m east, past the bus leg
    vote = vote_for(drift, path=path, steps=steps)
    assert vote.verdict == "problem" and vote.problem_type == "off_route"
    # Without the step, the drift is matched to the bus leg and looks fine
    assert vote_for(drift, path=path, steps=None, step_index=None).verdict == "normal"


def test_step_change_beyond_reach_trusts_the_step():
    # step 1 starts 1000 m along, far beyond reach of the last step 0 fix
    mid = xy(1000, 0)
    steps = [
        {"stepIndex": 0, "mode": "walk", "to": {"name": "Mid", "lat": mid[0], "lng": mid[1]}},
        {"stepIndex": 1, "mode": "walk", "to": {"name": "End", "lat": PATH[-1][0], "lng": PATH[-1][1]}}
    ]
    usable = [checkin(0, 100), checkin(1, 110), checkin(2, 1050, step=1)]
    ctx = TripContext(path=PATH, steps=steps, step_index=1, route_version=3, recent=usable)
    cum = cumulative_m(PATH)
    fixes = _locate(usable, ctx, cum, _step_ranges(steps, PATH, cum), SETTINGS)
    # matched inside step 1 where it is, not in the gap before it
    assert abs(fixes[-1].along_m - 1050) < 5 and fixes[-1].away_m < 5


# --- missed_stop ----------------------------------------------------------------------

# Walk 300 m north to a stop, bus 1000 m north to the next stop, then walk 500 m on
BUS_PATH = [xy(0, 0), xy(300, 0), xy(1300, 0), xy(1800, 0)]
BUS_STEPS = [
    {"stepIndex": 0, "mode": "walk", "to": {"name": "Stop A", "lat": BUS_PATH[1][0], "lng": BUS_PATH[1][1]}},
    {"stepIndex": 1, "mode": "bus", "to": {"name": "Stop B", "lat": BUS_PATH[2][0], "lng": BUS_PATH[2][1]}},
    {"stepIndex": 2, "mode": "walk", "to": {"name": "End", "lat": BUS_PATH[3][0], "lng": BUS_PATH[3][1]}},
]


def bus_vote(points, step_index=1, every=10):
    return vote_for(track(points, every=every, step=step_index), path=BUS_PATH, steps=BUS_STEPS, step_index=step_index)


def test_riding_past_the_stop_is_missed_stop():
    vote = bus_vote([(1000 + 80 * i, 0) for i in range(10)])  # 8 m/s, ends 420 m past Stop B
    assert vote.verdict == "problem" and vote.problem_type == "missed_stop"
    assert "past Stop B" in vote.reason and "step 1" in vote.reason


def test_bus_leaving_the_planned_path_after_the_stop_is_missed_stop():
    # Past Stop B the bus turns east, away from the walking path
    points = [(1100 + 80 * i, 0) for i in range(3)] + [(1300, 80 * i) for i in range(1, 8)]
    vote = bus_vote(points)
    assert vote.verdict == "problem" and vote.problem_type == "missed_stop"


def test_riding_towards_the_stop_is_normal():
    vote = bus_vote([(400 + 80 * i, 0) for i in range(10)])
    assert vote.verdict == "normal" and vote.confidence > 0.8


def test_walking_on_past_the_stop_is_unsure_normal():
    # Got off at Stop B and walked on, but the step hasn't advanced yet
    vote = bus_vote([(1300 + 13 * i, 0) for i in range(10, 20)])
    assert vote.verdict == "normal" and vote.confidence == 0.3
    assert "walking speed" in vote.reason


def test_same_position_on_a_walking_step_is_not_missed_stop():
    vote = bus_vote([(1000 + 80 * i, 0) for i in range(10)], step_index=2)
    assert vote.problem_type != "missed_stop"


# --- abstain --------------------------------------------------------------------------


def test_abstain_without_route():
    for path in (None, [xy(0, 0)]):
        vote = vote_for(track([(300, 200)] * 5), path=path)
        assert vote.verdict == "abstain" and vote.confidence == 0 and vote.problem_type is None
        assert "no route" in vote.reason


def test_abstain_with_too_few_usable_points():
    vote = vote_for(track([(300, 200)] * 2))
    assert vote.verdict == "abstain" and vote.reason == "only 2 usable check-in(s), need 3"


def test_poor_gps_points_are_skipped():
    recent = track([(300, 200)] * 4)
    recent[1] = recent[1].model_copy(update={"accuracy_m": 90})
    assert vote_for(recent).problem_type == "off_route"
    assert vote_for(recent[:3]).verdict == "abstain"  # only 2 usable left


def test_abstain_on_poor_current_accuracy():
    assert vote_for(track([(300, 200)] * 5, accuracy=85)).verdict == "abstain"


def test_abstain_on_route_version_mismatch():
    vote = vote_for(track([(300, 200)] * 5), route_version_mismatch=True)
    assert vote.verdict == "abstain" and "version mismatch" in vote.reason


def test_missing_or_odd_steps_still_judge_from_the_path():
    recent = track([(300, 200)] * 5)
    for steps, step_index in (([], None), (None, 0), ([{"stepIndex": 0}], 0), (WALK, 7), (["bad"], 0)):
        assert vote_for(recent, steps=steps, step_index=step_index).problem_type == "off_route"


# --- replay: each simulator scenario, one check-in at a time, sliding window of 10 -----


def replay(scenario):
    route = json.loads((FIXTURES / "route.json").read_text())
    path = [tuple(p) for p in route["path"]]
    rows = json.loads((FIXTURES / f"{scenario}.json").read_text())
    checkins = [CheckIn.model_validate(r) for r in rows]
    for i, row in enumerate(rows):
        ctx = TripContext(
            steps=route["steps"], path=path, step_index=row["stepIndex"], route_version=3,
            recent=checkins[max(0, i - 9) : i + 1],
        )
        request = DetectorRequest(request_id=f"{row['tripId']}#{row['seq']}", risk_level="normal", checkin=checkins[i])
        yield row["expected"], detect(request, ctx, SETTINGS)


@pytest.mark.parametrize("suffix", ["", "-browser"])
@pytest.mark.parametrize("scenario", ["off_route", "wrong_direction"])
def test_replay_problem_is_flagged_soon_and_only_as_itself(scenario, suffix):
    results = list(replay(scenario + suffix))
    first_label = next(i for i, (expected, _) in enumerate(results) if expected == scenario)
    first_flag = next(i for i, (_, vote) in enumerate(results) if vote.verdict == "problem")
    assert first_flag >= first_label, "flagged before the problem started"
    assert first_flag - first_label <= 6
    assert {v.problem_type for _, v in results if v.verdict == "problem"} == {scenario}


@pytest.mark.parametrize("suffix", ["", "-browser"])
@pytest.mark.parametrize("scenario", ["normal", "normal_bus_wait", "long_dwell"])
def test_replay_on_route_scenarios_never_flag(scenario, suffix):
    results = list(replay(scenario + suffix))
    assert not [v for _, v in results if v.verdict == "problem"]
    assert [v.verdict for _, v in results[:2]] == ["abstain", "abstain"]


@pytest.mark.parametrize("suffix", ["", "-browser"])
def test_replay_poor_gps_always_abstains(suffix):
    assert {v.verdict for _, v in replay("poor_gps" + suffix)} == {"abstain"}
