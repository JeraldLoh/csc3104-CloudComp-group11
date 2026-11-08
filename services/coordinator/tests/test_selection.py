import json
import pytest

from app.selection import (
    ALL_DETECTORS,
    DEFAULT_PAIR,
    choose_detectors,
    counted_votes,
    needs_escalation,
    parse_urls,
    remaining,
)


def vote(verdict: str) -> dict:
    return {"verdict": verdict}


# --- choose_detectors ---------------------------------------------------------

def test_adaptive_normal_risk_uses_the_diverse_pair():
    assert choose_detectors("normal") == ["route-corridor", "dwell-time"]


def test_adaptive_elevated_risk_uses_every_detector():
    assert choose_detectors("elevated") == list(ALL_DETECTORS)


def test_fixed2_ignores_risk():
    assert choose_detectors("elevated", mode="fixed2") == list(DEFAULT_PAIR)


def test_four_detectors_with_route_progress():
    assert ALL_DETECTORS == ("route-corridor", "heading-speed", "dwell-time", "route-progress")


def test_fixed4_ignores_risk():
    assert choose_detectors("normal", mode="fixed4") == list(ALL_DETECTORS)


def test_unknown_risk_level_is_treated_as_normal():
    assert choose_detectors("something-else") == list(DEFAULT_PAIR)


def test_custom_pair():
    assert choose_detectors("normal", pair=("heading-speed", "route-progress")) == ["heading-speed", "route-progress"]


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        choose_detectors("normal", mode="fixed3")


def test_default_pair_is_diverse():
    # Location-based and time-based, not two similar detectors.
    assert set(DEFAULT_PAIR) == {"route-corridor", "dwell-time"}


# --- votes and escalation -------------------------------------------------------

def test_abstains_are_not_counted():
    assert counted_votes([vote("problem"), vote("abstain"), vote("normal")]) == 2


def test_no_escalation_when_quorum_met():
    assert not needs_escalation([vote("problem"), vote("normal")], quorum=2)


def test_escalates_when_one_of_two_abstains():
    assert needs_escalation([vote("problem"), vote("abstain")], quorum=2)


def test_escalates_when_both_abstain():
    assert needs_escalation([vote("abstain"), vote("abstain")], quorum=2)


def test_never_escalates_beyond_every_detector():
    all_abstain = [vote("abstain")] * len(ALL_DETECTORS)
    assert not needs_escalation(all_abstain, quorum=2)


def test_remaining_detectors_keep_their_order():
    assert remaining(["route-corridor", "dwell-time"]) == ["heading-speed", "route-progress"]


def test_nothing_remaining_after_every_detector():
    assert remaining(list(ALL_DETECTORS)) == []


# --- parse_urls ----------------------------------------------------------------

def test_default_urls_use_service_names_from_the_contract():
    urls = parse_urls("")
    assert urls == {
        "route-corridor": "http://detector-route:8000",
        "heading-speed": "http://detector-heading:8000",
        "dwell-time": "http://detector-dwell:8000",
        "route-progress": "http://detector-progress:8000",
    }


def test_override_one_url_and_strip_trailing_slash():
    urls = parse_urls(" route-corridor = http://localhost:9001/ ")
    assert urls["route-corridor"] == "http://localhost:9001"
    assert urls["dwell-time"] == "http://detector-dwell:8000"


def test_unknown_detector_in_urls_is_rejected():
    with pytest.raises(ValueError):
        parse_urls("speed-detector=http://x:8000")


def test_missing_url_is_rejected():
    with pytest.raises(ValueError):
        parse_urls("route-corridor=")


# --- step-aware pairs -------------------------------------------------------------------------

from app.selection import RIDING_PAIR, UNKNOWN_STEP_PAIR, WAITING_PAIR, WALKING_PAIR, pair_for_step, step_mode  # noqa: E402

ROUTE = json.dumps({"steps": [{"stepIndex": 0, "mode": "walk"}, {"stepIndex": 1, "mode": "Bus"}], "path": []})


@pytest.mark.parametrize("index, mode", [(0, "walk"), (1, "bus"), ("1", "bus"), (2, None), (-1, None), (None, None)])
def test_step_mode(index, mode):
    assert step_mode(ROUTE, index) == mode


@pytest.mark.parametrize("route", [None, "", "{not json", '{"path": []}', '{"steps": "x"}', '{"steps": [5]}'])
def test_step_mode_of_a_bad_route_is_unknown(route):
    assert step_mode(route, 0) is None


@pytest.mark.parametrize("mode, speed, pair, situation", [
    ("walk", 1.3, WALKING_PAIR, "walking"),
    ("walk", None, WALKING_PAIR, "walking"),
    ("bus", 0.0, WAITING_PAIR, "waiting"),
    ("bus", 1.99, WAITING_PAIR, "waiting"),
    ("bus", 2.0, RIDING_PAIR, "riding"),
    ("train", 15, RIDING_PAIR, "riding"),
    ("mrt", 15, RIDING_PAIR, "riding"),
    ("bus", None, WAITING_PAIR, "waiting"),
    ("bus", "fast", WAITING_PAIR, "waiting"),
    ("ferry", 5, UNKNOWN_STEP_PAIR, "unknown step"),
    (None, 5, UNKNOWN_STEP_PAIR, "unknown step"),
])
def test_pair_for_step(mode, speed, pair, situation):
    assert pair_for_step(mode, speed) == (pair, situation)


def test_every_step_pair_includes_route_corridor_and_only_real_detectors():
    for pair in (WALKING_PAIR, WAITING_PAIR, RIDING_PAIR, UNKNOWN_STEP_PAIR):
        assert "route-corridor" in pair and len(pair) == 2 and set(pair) <= set(ALL_DETECTORS)
