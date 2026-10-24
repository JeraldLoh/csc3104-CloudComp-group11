import json
from pathlib import Path

import pytest

from app.voting import aggregate, vote_weight

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "examples"


def v(detector: str, verdict: str, confidence: float = 0.8, problem_type: str | None = None) -> dict:
    return {"detectorId": detector, "verdict": verdict, "confidence": confidence, "problemType": problem_type}


# --- basic outcomes ---------------------------------------------------------------

def test_both_say_problem():
    d = aggregate([v("route-corridor", "problem", 0.9, "off_route"), v("dwell-time", "problem", 0.7, "off_route")])
    assert d.decision == "problem"
    assert d.problem_type == "off_route"
    assert d.confidence == pytest.approx(1.0)
    assert d.counted == 2


def test_both_say_normal():
    d = aggregate([v("route-corridor", "normal", 0.9), v("dwell-time", "normal", 0.6)])
    assert d.decision == "normal"
    assert d.problem_type is None
    assert d.confidence == pytest.approx(1.0)
    assert d.problem_score == pytest.approx(0.0)


def test_confident_problem_beats_weak_normal():
    d = aggregate([v("route-corridor", "problem", 0.9, "off_route"), v("dwell-time", "normal", 0.3)])
    assert d.decision == "problem"
    assert d.problem_score == pytest.approx(0.9 / 1.2)


def test_confident_normal_beats_weak_problem():
    d = aggregate([v("route-corridor", "problem", 0.2, "off_route"), v("dwell-time", "normal", 0.9)])
    assert d.decision == "normal"
    assert d.confidence == pytest.approx(0.9 / 1.1)


def four(p1, p2, n1, n2) -> list[dict]:
    """Two problem votes and two normal votes with the given confidences."""
    return [
        v("route-corridor", "problem", p1, "off_route"),
        v("heading-speed", "problem", p2, "off_route"),
        v("dwell-time", "normal", n1),
        v("ml-anomaly", "normal", n2),
    ]


# --- close calls: ask more detectors instead of guessing -------------------------------

def test_one_vs_one_tie_asks_for_more_votes():
    d = aggregate([v("route-corridor", "problem", 0.8, "off_route"), v("dwell-time", "normal", 0.8)])
    assert d.decision == "inconclusive"
    assert d.reason == "split"
    assert d.needs_more_votes
    assert d.problem_score == pytest.approx(0.5)   # kept, for the dashboard


def test_near_tie_asks_for_more_votes():
    d = aggregate([v("route-corridor", "problem", 0.81, "off_route"), v("dwell-time", "normal", 0.79)])
    assert d.reason == "split"
    assert d.needs_more_votes


def test_score_at_edge_of_close_band_is_still_split():
    # 0.6 / (0.6 + 0.4) = 0.6, exactly threshold + margin
    d = aggregate([v("route-corridor", "problem", 0.6, "off_route"), v("dwell-time", "normal", 0.4)])
    assert d.reason == "split"


def test_score_outside_close_band_is_decided():
    # 0.7 / (0.7 + 0.3) = 0.7
    d = aggregate([v("route-corridor", "problem", 0.7, "off_route"), v("dwell-time", "normal", 0.3)])
    assert d.decision == "problem"
    assert not d.needs_more_votes


def test_two_vs_two_tie_with_all_four_counts_as_problem():
    d = aggregate(four(0.8, 0.8, 0.8, 0.8))
    assert d.decision == "problem"
    assert d.problem_score == pytest.approx(0.5)
    assert not d.needs_more_votes


def test_close_call_with_all_four_is_decided_by_the_threshold():
    d = aggregate(four(0.5, 0.4, 0.6, 0.5))   # 0.9 / 2.0 = 0.45
    assert d.decision == "normal"
    assert not d.needs_more_votes


def test_zero_margin_only_escalates_exact_ties():
    near = [v("route-corridor", "problem", 0.81, "off_route"), v("dwell-time", "normal", 0.79)]
    assert aggregate(near, close_margin=0).decision == "problem"
    exact = [v("route-corridor", "problem", 0.8, "off_route"), v("dwell-time", "normal", 0.8)]
    assert aggregate(exact, close_margin=0).reason == "split"


def test_higher_threshold_with_all_four_turns_a_tie_into_normal():
    assert aggregate(four(0.8, 0.8, 0.8, 0.8), threshold=0.6).decision == "normal"


# --- abstains and quorum -------------------------------------------------------------

def test_abstains_are_ignored():
    d = aggregate([
        v("route-corridor", "problem", 0.9, "off_route"),
        v("dwell-time", "abstain", 0.0),
        v("heading-speed", "problem", 0.6, "off_route"),
    ])
    assert d.decision == "problem"
    assert d.counted == 2


def test_one_real_vote_is_inconclusive():
    d = aggregate([v("route-corridor", "problem", 0.9, "off_route"), v("dwell-time", "abstain", 0.0)])
    assert d.decision == "inconclusive"
    assert d.reason == "quorum"
    assert d.needs_more_votes
    assert d.problem_score is None
    assert d.counted == 1


def test_all_abstain_is_inconclusive():
    assert aggregate([v("route-corridor", "abstain"), v("dwell-time", "abstain")]).decision == "inconclusive"


def test_too_few_votes_after_asking_all_four_is_final():
    d = aggregate([
        v("route-corridor", "problem", 0.9, "off_route"),
        v("dwell-time", "abstain"),
        v("heading-speed", "abstain"),
        v("ml-anomaly", "abstain"),
    ])
    assert d.decision == "inconclusive"
    assert not d.needs_more_votes   # nobody left to ask


def test_no_votes_is_inconclusive():
    assert aggregate([]).decision == "inconclusive"


def test_unknown_verdict_is_treated_like_an_abstain():
    d = aggregate([v("route-corridor", "maybe"), v("dwell-time", "normal")])
    assert d.decision == "inconclusive"


def test_all_zero_confidence_is_inconclusive():
    d = aggregate([v("route-corridor", "problem", 0.0, "off_route"), v("dwell-time", "normal", 0.0)])
    assert d.decision == "inconclusive"
    assert d.reason == "no_weight"


def test_quorum_of_one():
    d = aggregate([v("route-corridor", "problem", 0.9, "off_route")], quorum=1)
    assert d.decision == "problem"


# --- reliability weights (detector:weights) ------------------------------------------------

def test_unreliable_detector_is_outvoted():
    # dwell-time is known to be wrong (Member 4's fault mode); its weight is lowered.
    votes = [v("route-corridor", "problem", 0.5, "off_route"), v("dwell-time", "normal", 0.9)]
    assert aggregate(votes).decision == "normal"                                      # 0.5 / 1.4 = 0.36
    assert aggregate(votes, reliability={"dwell-time": 0.3}).decision == "problem"   # 0.5 / 0.77 = 0.65


def test_one_wrong_detector_out_of_four_is_outvoted():
    votes = [
        v("route-corridor", "problem", 0.8, "off_route"),
        v("heading-speed", "problem", 0.7, "wrong_direction"),
        v("ml-anomaly", "problem", 0.6, "off_route"),
        v("dwell-time", "normal", 1.0),   # the faulty one, fully confident
    ]
    d = aggregate(votes)
    assert d.decision == "problem"
    assert d.counted == 4


def test_missing_reliability_defaults_to_one():
    assert vote_weight(v("route-corridor", "problem", 0.8), {}) == pytest.approx(0.8)


def test_negative_reliability_counts_as_zero():
    assert vote_weight(v("route-corridor", "problem", 0.8), {"route-corridor": -1}) == 0.0


def test_confidence_outside_0_1_is_clamped():
    assert vote_weight(v("route-corridor", "problem", 1.7), {}) == 1.0
    assert vote_weight(v("route-corridor", "problem", -0.5), {}) == 0.0


def test_missing_or_bad_confidence_counts_as_zero():
    assert vote_weight({"detectorId": "route-corridor", "verdict": "problem"}, {}) == 0.0
    assert vote_weight({"detectorId": "route-corridor", "verdict": "problem", "confidence": "high"}, {}) == 0.0


# --- problem type ----------------------------------------------------------------------

def test_problem_type_with_most_weight_wins():
    d = aggregate([
        v("route-corridor", "problem", 0.9, "off_route"),
        v("heading-speed", "problem", 0.5, "wrong_direction"),
        v("ml-anomaly", "problem", 0.3, "wrong_direction"),
    ])
    assert d.problem_type == "off_route"   # 0.9 vs 0.8


def test_problem_type_tie_is_alphabetical():
    d = aggregate([
        v("route-corridor", "problem", 0.5, "off_route"),
        v("heading-speed", "problem", 0.5, "missed_stop"),
    ])
    assert d.problem_type == "missed_stop"


def test_problem_without_a_type_gives_none():
    d = aggregate([v("route-corridor", "problem", 0.9), v("dwell-time", "problem", 0.9)])
    assert d.decision == "problem"
    assert d.problem_type is None


# --- contract example ---------------------------------------------------------------------

def test_votes_from_the_contract_example():
    report = json.loads((CONTRACTS / "problem-report.json").read_text())
    d = aggregate(report["votes"])
    assert d.decision == "problem"
    assert d.counted == 4
    # 3 of 4 detectors say problem: (0.85 + 0.78 + 0.7) / (0.85 + 0.78 + 0.6 + 0.7)
    assert d.problem_score == pytest.approx(2.33 / 2.93)


def test_detector_vote_example_is_counted():
    vote = json.loads((CONTRACTS / "detector-vote.json").read_text())
    d = aggregate([vote, {**vote, "detectorId": "dwell-time"}])
    assert d.decision == "problem"
    assert d.problem_type == vote["problemType"]
