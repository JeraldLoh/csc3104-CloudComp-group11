import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from detectors.config import Settings
from detectors.context import TripContext
from detectors.heading_speed import detect
from detectors.models import CheckIn, DetectorRequest

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = Settings()  # moving >= 0.5 m/s, wrong direction > 120 deg, GPS limit 50 m
T0 = datetime(2026, 10, 12, 8, 0, 0, tzinfo=timezone.utc)
STEP_DEG = 13 / 111_195  # ~13 m of latitude, one walking step of 10 s

# A straight path heading due north (bearing 0)
PATH = [(1.40, 103.9), (1.41, 103.9)]
STEPS = [{"stepIndex": 0, "mode": "walk"}, {"stepIndex": 1, "mode": "bus"}]


def checkin(seq, lat, speed=1.3, accuracy=10.0, heading=0.0):
    return CheckIn.model_validate(
        {
            "tripId": "T-001",
            "travellerId": "U-042",
            "seq": seq,
            "timestamp": (T0 + timedelta(seconds=10 * seq)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "lat": lat,
            "lng": 103.9,
            "accuracyM": accuracy,
            "speedMps": speed,
            "headingDeg": heading,
            "stepIndex": 0,
            "routeVersion": 3,
            "status": "active",
        }
    )


def walk(direction, count=6, start=1.405, **kwargs):
    """``count`` check-ins moving north (+1) or south (-1) along the path."""
    heading = 0.0 if direction > 0 else 180.0
    return [checkin(i + 1, start + direction * i * STEP_DEG, heading=heading, **kwargs) for i in range(count)]


def vote_for(recent, path=PATH, step_index=0, **ctx_kwargs):
    ctx = TripContext(path=path, steps=STEPS, step_index=step_index, route_version=3, recent=recent, **ctx_kwargs)
    request = DetectorRequest(request_id="T-001#x", risk_level="normal", checkin=recent[-1])
    return detect(request, ctx, SETTINGS)


def test_moving_along_route_is_normal():
    vote = vote_for(walk(+1))
    assert vote.verdict == "normal" and vote.problem_type is None
    assert vote.detector_id == "heading-speed"
    assert 0.5 <= vote.confidence <= 0.95
    assert "moving 0° off" in vote.reason


def test_moving_against_route_is_wrong_direction():
    vote = vote_for(walk(-1))
    assert vote.verdict == "problem" and vote.problem_type == "wrong_direction"
    assert vote.reason == "moving 180° off the route direction (limit 120°) over 6 moving check-ins"
    assert 0.5 <= vote.confidence <= 0.95


def test_sideways_is_not_wrong_direction():
    # 90 degrees off the path is off-route's business, not this detector's
    sideways = [checkin(i, 1.405, heading=90) for i in range(1, 7)]
    sideways = [c.model_copy(update={"lng": 103.9 + i * STEP_DEG}) for i, c in enumerate(sideways)]
    vote = vote_for(sideways)
    assert vote.verdict == "normal" and "moving 90° off" in vote.reason


def test_median_ignores_a_few_noisy_bearings():
    recent = walk(+1, count=8)
    recent[3] = recent[3].model_copy(update={"lat": recent[3].lat - 3 * STEP_DEG})  # one bad fix
    assert vote_for(recent).verdict == "normal"


def test_confidence_grows_with_more_moving_points():
    few = vote_for(walk(-1, count=4))
    many = vote_for(walk(-1, count=9))
    assert many.confidence > few.confidence


def test_stationary_abstains():
    vote = vote_for(walk(+1, speed=0.1))
    assert vote.verdict == "abstain" and vote.confidence == 0 and vote.problem_type is None
    assert vote.reason == "not moving enough to judge direction (0 of 6 check-ins moving, need 3)"


def test_too_few_moving_points_abstains():
    recent = walk(+1, count=6, speed=0.1)
    recent[-2:] = [recent[-2].model_copy(update={"speed_mps": 1.3}), recent[-1].model_copy(update={"speed_mps": 1.3})]
    assert vote_for(recent).verdict == "abstain"


def test_falls_back_to_heading_when_fixes_too_close():
    # Same spot, but speed says moving and heading says due south: use headingDeg
    recent = [checkin(i, 1.405, heading=180) for i in range(1, 6)]
    vote = vote_for(recent)
    assert vote.verdict == "problem" and "180°" in vote.reason


def test_fast_walking_noted_in_reason_only():
    vote = vote_for(walk(+1, speed=6.0))
    assert vote.verdict == "normal"
    assert vote.reason.endswith("; speed 6.0 m/s is high for walking")
    # Same speed on a bus step is unremarkable
    bus = vote_for(walk(+1, speed=6.0), step_index=1)
    assert "high for walking" not in bus.reason


def test_poor_gps_points_are_skipped():
    recent = walk(-1)
    recent[2] = recent[2].model_copy(update={"accuracy_m": 90.0})
    vote = vote_for(recent)
    assert vote.verdict == "problem" and vote.reason.endswith("over 5 moving check-ins")


def test_abstain_without_route():
    for path in (None, [(1.4, 103.9)]):
        vote = vote_for(walk(-1), path=path)
        assert vote.verdict == "abstain" and "no route" in vote.reason


def test_abstain_on_poor_current_accuracy():
    recent = walk(-1)
    recent[-1] = recent[-1].model_copy(update={"accuracy_m": 85.0})
    vote = vote_for(recent)
    assert vote.verdict == "abstain" and vote.reason == "GPS accuracy 85 m exceeds 50 m limit"


def test_abstain_on_route_version_mismatch():
    vote = vote_for(walk(-1), route_version_mismatch=True)
    assert vote.verdict == "abstain" and "version mismatch" in vote.reason


# Replay: feed each simulator scenario one check-in at a time, with a sliding window

def replay(scenario):
    route = json.loads((FIXTURES / "route.json").read_text())
    path = [tuple(p) for p in route["path"]]
    rows = json.loads((FIXTURES / f"{scenario}.json").read_text())
    checkins = [CheckIn.model_validate(r) for r in rows]
    for i, row in enumerate(rows):
        window = checkins[max(0, i - 9) : i + 1]  # trip:{id}:recent holds 10
        ctx = TripContext(steps=route["steps"], path=path, step_index=row["stepIndex"], route_version=3, recent=window)
        request = DetectorRequest(request_id=f"{row['tripId']}#{row['seq']}", risk_level="normal", checkin=checkins[i])
        yield row["expected"], window, detect(request, ctx, SETTINGS)


def test_replay_wrong_direction_is_flagged_soon_after_turning_back():
    results = list(replay("wrong_direction"))
    first_back = next(i for i, (expected, _, _) in enumerate(results) if expected == "wrong_direction")
    first_flag = next(i for i, (_, _, vote) in enumerate(results) if vote.verdict == "problem")
    assert all(v.problem_type == "wrong_direction" for _, _, v in results if v.verdict == "problem")
    assert first_flag >= first_back, "flagged before the traveller turned around"
    assert first_flag - first_back <= 6
    assert results[-1][2].verdict == "problem"


@pytest.mark.parametrize("scenario", ["normal", "normal_bus_wait", "long_dwell", "off_route"])
def test_replay_other_scenarios_never_flag(scenario):
    results = list(replay(scenario))
    assert not [v for _, _, v in results if v.verdict == "problem"]
    assert {v.verdict for _, _, v in results} <= {"normal", "abstain"}


@pytest.mark.parametrize("scenario", ["normal_bus_wait", "long_dwell"])
def test_replay_abstains_while_stationary(scenario):
    results = list(replay(scenario))
    still = [
        v
        for _, window, v in results
        if len(window) == 10 and all(c.speed_mps < SETTINGS.min_moving_speed_mps for c in window)
    ]
    assert len(still) >= 20
    assert {v.verdict for v in still} == {"abstain"}
    assert "normal" in {v.verdict for _, _, v in results}  # and it judges once moving again


def test_replay_poor_gps_always_abstains():
    assert {v.verdict for _, _, v in replay("poor_gps")} == {"abstain"}


# Web-app check-ins: the browser often reports no speed, so movement is judged over >= 30 s

def no_speed(checkins):
    return [c.model_copy(update={"speed_mps": None, "heading_deg": None}) for c in checkins]


def test_no_speed_moving_along_route_is_normal():
    vote = vote_for(no_speed(walk(+1, count=7)))
    assert vote.verdict == "normal" and "moving 0° off" in vote.reason


def test_no_speed_moving_against_route_is_wrong_direction():
    vote = vote_for(no_speed(walk(-1, count=7)))
    assert vote.verdict == "problem" and vote.problem_type == "wrong_direction"


def test_no_speed_standing_still_with_gps_jitter_abstains():
    jitter = [0.00004, -0.00003, 0.00005, -0.00006, 0.00002, -0.00005, 0.00004]  # up to ~7 m
    recent = [checkin(i + 1, 1.405 + j, accuracy=10.0) for i, j in enumerate(jitter)]
    vote = vote_for(no_speed(recent))
    assert vote.verdict == "abstain" and "not moving enough" in vote.reason


def test_no_speed_needs_a_fix_30s_back():
    # 4 check-ins span only 30 s, so just the last one has a baseline: 1 moving point
    vote = vote_for(no_speed(walk(-1, count=4)))
    assert vote.verdict == "abstain"


def test_no_speed_irregular_gaps_use_timestamps_not_counts():
    # Check-ins 40 s apart (a throttled tab): each one after the first has a 30 s baseline
    def sparse(n):
        return [checkin(i * 4, 1.405 - i * 4 * STEP_DEG, speed=None, heading=None) for i in range(1, n + 1)]

    assert vote_for(sparse(4)).verdict == "problem"  # 3 moving check-ins from only 4 received
    assert vote_for(sparse(3)).verdict == "abstain"  # only 2 moving


def test_replay_browser_wrong_direction_is_flagged():
    results = list(replay("wrong_direction-browser"))
    first_back = next(i for i, (expected, _, _) in enumerate(results) if expected == "wrong_direction")
    first_flag = next(i for i, (_, _, vote) in enumerate(results) if vote.verdict == "problem")
    assert all(v.problem_type == "wrong_direction" for _, _, v in results if v.verdict == "problem")
    assert first_flag >= first_back
    assert first_flag - first_back <= 8  # a bit slower than clean data: missing check-ins, 30 s baseline
    assert results[-1][2].verdict == "problem"


@pytest.mark.parametrize("scenario", ["normal", "normal_bus_wait", "long_dwell", "off_route"])
def test_replay_browser_other_scenarios_never_flag(scenario):
    results = list(replay(f"{scenario}-browser"))
    assert not [v for _, _, v in results if v.verdict == "problem"]
    assert "normal" in {v.verdict for _, _, v in results}


def test_replay_browser_poor_gps_always_abstains():
    assert {v.verdict for _, _, v in replay("poor_gps-browser")} == {"abstain"}
