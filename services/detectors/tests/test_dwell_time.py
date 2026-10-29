import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from detectors.config import Settings
from detectors.context import TripContext
from detectors.dwell_time import detect
from detectors.models import CheckIn, DetectorRequest

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = Settings()  # radius 30 m, walk limit 300 s, transit limit 1200 s
T0 = datetime(2026, 10, 12, 8, 0, 0, tzinfo=timezone.utc)
M = 1 / 111_195  # one metre of latitude, in degrees

PATH = [(1.40, 103.9), (1.41, 103.9)]
STEPS = [{"stepIndex": 0, "mode": "walk"}, {"stepIndex": 1, "mode": "bus"}, {"stepIndex": 2, "mode": "weird"}]


def checkin(seq, lat=1.405, every=10, accuracy=10.0):
    return CheckIn.model_validate(
        {
            "tripId": "T-001",
            "travellerId": "U-042",
            "seq": seq,
            "timestamp": (T0 + timedelta(seconds=every * seq)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "lat": lat,
            "lng": 103.9,
            "accuracyM": accuracy,
            "stepIndex": 0,
            "routeVersion": 3,
            "status": "active",
        }
    )


def still(count, every=10, **kwargs):
    return [checkin(i, every=every, **kwargs) for i in range(count)]


def vote_for(recent, step_index=0, steps=STEPS, **ctx_kwargs):
    ctx = TripContext(path=PATH, steps=steps, step_index=step_index, route_version=3, recent=recent, **ctx_kwargs)
    request = DetectorRequest(request_id="T-001#x", risk_level="normal", checkin=recent[-1])
    return detect(request, ctx, SETTINGS)


def test_long_stationary_walk_is_long_dwell():
    vote = vote_for(still(10, every=40))  # 360 s > 300 s
    assert vote.verdict == "problem" and vote.problem_type == "long_dwell"
    assert vote.detector_id == "dwell-time"
    assert vote.reason == "stationary for 360 s within 30 m (limit 300 s)"
    assert 0.5 <= vote.confidence <= 0.95


def test_confidence_grows_with_dwell():
    assert vote_for(still(10, every=80)).confidence > vote_for(still(10, every=40)).confidence


def test_transit_step_uses_the_longer_limit():
    # 360 s is too long to walk but fine at a bus stop (only the short window keeps it from "normal")
    vote = vote_for(still(10, every=40), step_index=1)
    assert vote.verdict == "abstain" and "limit 1200 s" in vote.reason
    assert vote_for(still(10, every=140), step_index=1).problem_type == "long_dwell"  # 1260 s


def test_unknown_mode_or_step_uses_walk_limit():
    for step_index in (2, 9, None):
        assert vote_for(still(10, every=40), step_index=step_index).problem_type == "long_dwell"
    assert vote_for(still(10, every=40), steps=None).problem_type == "long_dwell"


def test_short_window_near_limit_abstains():
    # 90 s of standing still is 30% of the walk limit: could be much longer
    vote = vote_for(still(10))
    assert vote.verdict == "abstain" and vote.confidence == 0 and vote.problem_type is None
    assert vote.reason == "window too short to judge: stationary for the whole 90 s window (limit 300 s)"


def test_short_window_far_from_limit_is_normal():
    # Same 90 s window at a bus stop is 7.5% of the limit
    vote = vote_for(still(10), step_index=1)
    assert vote.verdict == "normal"


def test_moving_traveller_is_normal():
    recent = [checkin(i, lat=1.405 + i * 80 * M) for i in range(10)]  # 80 m per 10 s
    vote = vote_for(recent)
    assert vote.verdict == "normal" and vote.reason == "stationary for 0 s (limit 300 s)"


def test_only_the_trailing_stationary_run_counts():
    moving = [checkin(i, lat=1.405 + i * 40 * M) for i in range(5)]
    stopped = [checkin(i, lat=1.405 + 5 * 40 * M, every=10) for i in range(5, 10)]
    vote = vote_for(moving + stopped)
    assert vote.verdict == "normal" and "stationary for 40 s" in vote.reason or "stationary for 50 s" in vote.reason


def test_gps_jitter_within_radius_still_counts_as_stationary():
    recent = [checkin(i, lat=1.405 + (10 if i % 2 else -10) * M, every=40) for i in range(10)]
    assert vote_for(recent).problem_type == "long_dwell"


def test_poor_gps_points_are_skipped():
    recent = still(10, every=40)
    recent[2] = recent[2].model_copy(update={"accuracy_m": 90.0, "lat": 1.41})  # wild, untrusted fix
    assert vote_for(recent).problem_type == "long_dwell"


def test_abstain_without_route():
    ctx = TripContext(path=None, steps=STEPS, step_index=0, recent=still(10, every=40))
    request = DetectorRequest(request_id="T-001#x", risk_level="normal", checkin=ctx.recent[-1])
    vote = detect(request, ctx, SETTINGS)
    assert vote.verdict == "abstain" and "no route" in vote.reason


def test_abstain_on_poor_current_accuracy():
    recent = still(10, every=40)
    recent[-1] = recent[-1].model_copy(update={"accuracy_m": 85.0})
    vote = vote_for(recent)
    assert vote.verdict == "abstain" and vote.reason == "GPS accuracy 85 m exceeds 50 m limit"


def test_abstain_on_route_version_mismatch():
    vote = vote_for(still(10, every=40), route_version_mismatch=True)
    assert vote.verdict == "abstain" and "version mismatch" in vote.reason


# Replay: feed each simulator scenario one check-in at a time, with a sliding window

def replay(scenario, window_size):
    route = json.loads((FIXTURES / "route.json").read_text())
    path = [tuple(p) for p in route["path"]]
    rows = json.loads((FIXTURES / f"{scenario}.json").read_text())
    checkins = [CheckIn.model_validate(r) for r in rows]
    for i, row in enumerate(rows):
        window = checkins[max(0, i - window_size + 1) : i + 1]
        ctx = TripContext(steps=route["steps"], path=path, step_index=row["stepIndex"], route_version=3, recent=window)
        request = DetectorRequest(request_id=f"{row['tripId']}#{row['seq']}", risk_level="normal", checkin=checkins[i])
        yield row["expected"], detect(request, ctx, SETTINGS)


SUFFIXES = ["", "-browser"]


@pytest.mark.parametrize("suffix", SUFFIXES)
def test_replay_long_dwell_is_flagged_with_a_long_enough_window(suffix):
    # 40 check-ins is what a dwellStartAt-style history would give; the 10-point window can't
    results = list(replay("long_dwell" + suffix, 40))
    flagged = [i for i, (_, v) in enumerate(results) if v.problem_type == "long_dwell"]
    labels = [expected for expected, _ in results]
    end = len(labels) - labels[::-1].index("long_dwell")  # first check-in after the dwell
    assert flagged and min(flagged) >= labels.index("long_dwell") - 3  # slowing down first
    # the window still holds the dwell for a few check-ins after the traveller moves off
    assert max(flagged) < end + 3


@pytest.mark.parametrize("suffix", SUFFIXES)
def test_replay_long_dwell_with_real_window_never_guesses(suffix):
    # With trip:{id}:recent's 10 points the dwell is undetectable: abstain, never a false flag
    results = list(replay("long_dwell" + suffix, 10))
    assert all(v.verdict != "problem" for _, v in results)
    dwelling = [v for expected, v in results if expected == "long_dwell"]
    assert "abstain" in {v.verdict for v in dwelling}


@pytest.mark.parametrize("window_size", [10, 40])
@pytest.mark.parametrize("scenario", ["normal", "normal_bus_wait", "normal_bus_wait-browser", "off_route", "poor_gps"])
def test_replay_other_scenarios_never_flag(scenario, window_size):
    assert not [v for _, v in replay(scenario, window_size) if v.verdict == "problem"]


def test_train_step_uses_the_transit_limit():
    # MRT steps are "train" in the contract; waiting there is not a walking dwell
    steps = [{"stepIndex": 0, "mode": "train"}]
    vote = vote_for(still(10, every=40), steps=steps)
    assert "limit 1200 s" in vote.reason and vote.verdict != "problem"
