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


def test_adaptive_elevated_risk_uses_all_four():
    assert choose_detectors("elevated") == list(ALL_DETECTORS)


def test_fixed2_ignores_risk():
    assert choose_detectors("elevated", mode="fixed2") == list(DEFAULT_PAIR)


def test_fixed4_ignores_risk():
    assert choose_detectors("normal", mode="fixed4") == list(ALL_DETECTORS)


def test_unknown_risk_level_is_treated_as_normal():
    assert choose_detectors("something-else") == list(DEFAULT_PAIR)


def test_custom_pair():
    assert choose_detectors("normal", pair=("heading-speed", "ml-anomaly")) == ["heading-speed", "ml-anomaly"]


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


def test_never_escalates_beyond_all_four():
    four_abstains = [vote("abstain")] * 4
    assert not needs_escalation(four_abstains, quorum=2)


def test_remaining_detectors_keep_their_order():
    assert remaining(["route-corridor", "dwell-time"]) == ["heading-speed", "ml-anomaly"]


def test_nothing_remaining_after_all_four():
    assert remaining(list(ALL_DETECTORS)) == []


# --- parse_urls ----------------------------------------------------------------

def test_default_urls_use_service_names_from_the_contract():
    urls = parse_urls("")
    assert urls == {
        "route-corridor": "http://detector-route:8000",
        "heading-speed": "http://detector-heading:8000",
        "dwell-time": "http://detector-dwell:8000",
        "ml-anomaly": "http://detector-ml:8000",
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
