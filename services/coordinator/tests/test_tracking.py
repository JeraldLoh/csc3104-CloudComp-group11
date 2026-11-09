"""tracking.py: which check-ins leave problem types unchecked, and why. Pure, no I/O."""
import json

import pytest

from app.tracking import ALL_TYPES, failed_types, poor_gps, seconds_since, step_underground, unchecked, untrackable
from app.voting import Decision

INCONCLUSIVE = Decision("inconclusive", None, 0.0, None, 0, "quorum")
NORMAL = Decision("normal", None, 1.0, 0.0, 2)


def route(*steps):
    return json.dumps({"steps": [{"stepIndex": i, **s} for i, s in enumerate(steps)]})


@pytest.mark.parametrize("accuracy, poor", [(12, False), (50, False), (51, True), (None, True), ("x", True)])
def test_poor_gps_is_worse_than_the_detectors_limit_or_missing(accuracy, poor):
    assert poor_gps({"accuracyM": accuracy}, 50) is poor


def test_missing_accuracy_field_is_poor():
    assert poor_gps({}, 50) is True


@pytest.mark.parametrize("step, underground", [
    ({"mode": "walk", "underground": False}, False),
    ({"mode": "train", "underground": True}, True),
    ({"mode": "train", "underground": None}, True),    # OneMap doesn't say: treated as a tunnel
    ({"mode": "bus", "underground": None}, False),
    ({"mode": "train", "underground": False}, False),  # an above-ground stretch
])
def test_underground_like_the_heartbeat_monitor(step, underground):
    assert step_underground(route(step), 0) is underground


@pytest.mark.parametrize("route_json, step", [(None, 0), ("{not json", 0), (route({"mode": "train"}), 5),
                                              (route({"mode": "train"}), None)])
def test_unknown_step_is_above_ground(route_json, step):
    # Can't tell: assume above ground, so lost tracking is still reported.
    assert step_underground(route_json, step) is False


def test_untrackable_needs_inconclusive_poor_gps_and_above_ground():
    walk = route({"mode": "walk", "underground": False})
    poor = {"accuracyM": 90, "stepIndex": 0}
    assert untrackable(INCONCLUSIVE, poor, walk, 50)
    assert not untrackable(NORMAL, poor, walk, 50)                       # someone could judge
    assert not untrackable(INCONCLUSIVE, {**poor, "accuracyM": 10}, walk, 50)   # inconclusive for another reason
    assert not untrackable(INCONCLUSIVE, poor, route({"mode": "train", "underground": True}), 50)


def votes(**verdicts):
    """detectorId=verdict; "down" is a failed call as detectors.py records it."""
    return [{"detectorId": d.replace("_", "-"), "verdict": "abstain" if v == "down" else v,
             "reason": "error: ConnectError" if v == "down" else "test"} for d, v in verdicts.items()]


ALL_UP = dict(route_corridor="normal", heading_speed="normal", dwell_time="normal", route_progress="normal")


@pytest.mark.parametrize("down, types", [
    ((), []),
    (("dwell_time",), ["long_dwell"]),                          # its only detector
    (("route_corridor",), []),                                  # route-progress still checks off_route
    (("route_progress",), ["missed_stop"]),                     # the others still have a judge
    (("route_corridor", "route_progress"), ["missed_stop", "off_route"]),
    (("route_corridor", "heading_speed", "dwell_time", "route_progress"),
     ["long_dwell", "missed_stop", "off_route", "wrong_direction"]),
])
def test_failed_types_are_those_whose_every_detector_failed(down, types):
    assert failed_types(votes(**{**ALL_UP, **{d: "down" for d in down}})) == types


def test_a_detector_that_wasnt_asked_hasnt_failed():
    # adaptive: only a pair asked; dwell-time not asked is a choice, so long_dwell isn't counted
    assert failed_types(votes(route_corridor="down", heading_speed="normal")) == ["off_route"]


def test_a_silenced_detector_leaves_its_types_unchecked_like_a_failed_one():
    assert failed_types(votes(**ALL_UP), silenced=("dwell-time",)) == ["long_dwell"]
    assert failed_types(votes(**ALL_UP), silenced=("route-corridor",)) == []   # route-progress still checks
    assert failed_types(votes(**ALL_UP), silenced=("heading-speed", "route-progress")) == \
        ["missed_stop", "wrong_direction"]


def test_unchecked_says_whether_detectors_failed_or_were_silenced():
    good = {"accuracyM": 10, "stepIndex": 0}
    walk = route({"mode": "walk", "underground": False})
    silenced_dwell = Decision("normal", None, 1.0, 0.0, 3, silenced=("dwell-time",))
    assert unchecked(silenced_dwell, good, votes(**ALL_UP), walk, 50) == ("detectors silenced", ["long_dwell"])
    # Any real failure among them: "failed" (that one needs fixing, not just un-silencing).
    both = votes(**{**ALL_UP, "route_progress": "down"})
    assert unchecked(silenced_dwell, good, both, walk, 50) == ("detectors failed", ["long_dwell", "missed_stop"])


def test_cant_judge_yet_is_not_a_failure():
    assert failed_types(votes(**{**ALL_UP, "dwell_time": "abstain"})) == []


def test_unchecked_gives_the_reason_and_the_types():
    walk = route({"mode": "walk", "underground": False})
    poor, good = {"accuracyM": 90, "stepIndex": 0}, {"accuracyM": 10, "stepIndex": 0}
    assert unchecked(INCONCLUSIVE, good, [], None, 50) == ("route missing", ALL_TYPES)
    assert unchecked(INCONCLUSIVE, poor, [], walk, 50) == ("poor GPS", ALL_TYPES)
    assert unchecked(NORMAL, good, votes(**{**ALL_UP, "dwell_time": "down"}), None, 50) == \
        ("detectors failed", ["long_dwell"])   # a normal decision isn't "route missing"
    assert unchecked(NORMAL, good, votes(**ALL_UP), walk, 50) is None


def test_seconds_since():
    assert seconds_since("2026-10-12T08:00:00Z", "2026-10-12T08:02:00Z") == 120
    assert seconds_since("garbage", "2026-10-12T08:02:00Z") == 0
