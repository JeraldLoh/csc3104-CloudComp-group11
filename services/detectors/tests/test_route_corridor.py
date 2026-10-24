import json
from pathlib import Path

import pytest

from detectors.config import Settings
from detectors.context import TripContext
from detectors.models import CheckIn, DetectorRequest
from detectors.route_corridor import detect

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = Settings()  # corridor 100 m, 3 consecutive, GPS limit 50 m
M_PER_DEG_LNG = 111_190  # at ~1.4 N: 111_195 * cos(1.4 deg)

# A straight north-south path along lng 103.9; "east_m" below is the distance from it
PATH = [(1.40, 103.9), (1.41, 103.9)]


def checkin(seq, east_m=0.0, accuracy=10.0, route_version=3):
    return CheckIn.model_validate(
        {
            "tripId": "T-001",
            "travellerId": "U-042",
            "seq": seq,
            "timestamp": f"2026-10-12T08:00:{seq:02d}Z",
            "lat": 1.405,
            "lng": 103.9 + east_m / M_PER_DEG_LNG,
            "accuracyM": accuracy,
            "speedMps": 1.3,
            "headingDeg": 0,
            "stepIndex": 0,
            "routeVersion": route_version,
            "status": "active",
        }
    )


def vote_for(recent, path=PATH, **ctx_kwargs):
    ctx = TripContext(path=path, steps=[], route_version=3, recent=recent, **ctx_kwargs)
    request = DetectorRequest(request_id="T-001#x", risk_level="normal", checkin=recent[-1])
    return detect(request, ctx, SETTINGS)


def test_on_route_is_normal():
    vote = vote_for([checkin(i, east_m=5) for i in range(1, 5)])
    assert vote.verdict == "normal" and vote.problem_type is None
    assert vote.detector_id == "route-corridor"
    assert 0.5 <= vote.confidence <= 0.95


def test_three_consecutive_outside_is_off_route():
    vote = vote_for([checkin(i, east_m=180) for i in range(1, 4)])
    assert vote.verdict == "problem" and vote.problem_type == "off_route"
    assert vote.reason == "180 m from planned route (limit 110 m) for 3 check-ins"
    assert 0.5 <= vote.confidence <= 0.95


def test_limit_includes_gps_accuracy():
    # 140 m is outside 100 m but inside 100 + 45 m
    assert vote_for([checkin(i, east_m=140, accuracy=45) for i in range(1, 4)]).verdict == "normal"
    assert vote_for([checkin(i, east_m=140, accuracy=10) for i in range(1, 4)]).verdict == "problem"


def test_single_outlier_is_normal():
    recent = [checkin(1, 5), checkin(2, 5), checkin(3, 300)]
    vote = vote_for(recent)
    assert vote.verdict == "normal"
    assert "only 1 of the last 3" in vote.reason


def test_two_outside_is_still_normal():
    vote = vote_for([checkin(1, 5), checkin(2, 300), checkin(3, 300)])
    assert vote.verdict == "normal"


def test_confidence_grows_with_distance_and_agreement():
    near = vote_for([checkin(i, east_m=125) for i in range(1, 4)])
    far = vote_for([checkin(i, east_m=400) for i in range(1, 4)])
    long_run = vote_for([checkin(i, east_m=125) for i in range(1, 8)])
    assert far.confidence > near.confidence
    assert long_run.confidence > near.confidence
    assert far.confidence <= 0.95


def test_poor_gps_points_are_skipped_not_counted():
    recent = [checkin(1, 300), checkin(2, 300), checkin(3, 300, accuracy=90), checkin(4, 300)]
    vote = vote_for(recent)
    assert vote.verdict == "problem"
    assert vote.reason.endswith("for 3 check-ins")


def test_abstain_without_route():
    for path in (None, [(1.4, 103.9)]):
        vote = vote_for([checkin(i, 300) for i in range(1, 4)], path=path)
        assert vote.verdict == "abstain" and vote.confidence == 0 and vote.problem_type is None
        assert "no route" in vote.reason


def test_abstain_on_poor_current_accuracy():
    vote = vote_for([checkin(1, 300), checkin(2, 300), checkin(3, 300, accuracy=85)])
    assert vote.verdict == "abstain"
    assert vote.reason == "GPS accuracy 85 m exceeds 50 m limit"


def test_abstain_when_accuracy_missing():
    c = checkin(3, 300).model_copy(update={"accuracy_m": None})
    assert vote_for([checkin(1, 300), checkin(2, 300), c]).verdict == "abstain"


def test_abstain_on_route_version_mismatch():
    vote = vote_for([checkin(i, 300) for i in range(1, 4)], route_version_mismatch=True)
    assert vote.verdict == "abstain" and "version mismatch" in vote.reason


def test_abstain_with_too_few_usable_points():
    vote = vote_for([checkin(1, 300), checkin(2, 300)])
    assert vote.verdict == "abstain"
    assert vote.reason == "only 2 usable check-in(s), need 3"


# Replay: feed each simulator scenario one check-in at a time, with a sliding window

def replay(scenario):
    route = json.loads((FIXTURES / "route.json").read_text())
    path = [tuple(p) for p in route["path"]]
    rows = json.loads((FIXTURES / f"{scenario}.json").read_text())
    checkins = [CheckIn.model_validate(r) for r in rows]
    for i, row in enumerate(rows):
        window = checkins[max(0, i - 9) : i + 1]  # trip:{id}:recent holds 10
        ctx = TripContext(steps=route["steps"], path=path, route_version=3, recent=window)
        request = DetectorRequest(request_id=f"{row['tripId']}#{row['seq']}", risk_level="normal", checkin=checkins[i])
        yield row["expected"], detect(request, ctx, SETTINGS)


def test_replay_off_route_is_flagged_soon_after_drift_starts():
    results = list(replay("off_route"))
    first_drift = next(i for i, (expected, _) in enumerate(results) if expected == "off_route")
    first_flag = next(i for i, (_, vote) in enumerate(results) if vote.verdict == "problem")
    assert all(v.problem_type == "off_route" for _, v in results if v.verdict == "problem")
    assert first_flag >= first_drift, "flagged before the traveller was actually off route"
    assert first_flag - first_drift <= 6
    assert all(v.verdict == "problem" for _, v in results[first_flag:])


@pytest.mark.parametrize("scenario", ["normal", "normal_bus_wait", "long_dwell", "wrong_direction"])
def test_replay_on_route_scenarios_never_flag(scenario):
    results = list(replay(scenario))
    assert not [v for _, v in results if v.verdict == "problem"]
    # Only the first 2 check-ins lack the 3 usable points needed to judge
    assert [v.verdict for _, v in results[:2]] == ["abstain", "abstain"]
    assert {v.verdict for _, v in results[2:]} == {"normal"}


def test_replay_poor_gps_always_abstains():
    assert {v.verdict for _, v in replay("poor_gps")} == {"abstain"}
