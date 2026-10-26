import json
from pathlib import Path

import pytest

from app.voting import ALL_DETECTORS, aggregate, checks, vote_weight

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "examples"

# Who checks what (Member 4's detectors): route -> off_route, heading -> wrong_direction,
# dwell -> long_dwell, ml -> anything.
ROUTE, HEADING, DWELL, ML = ALL_DETECTORS


def v(detector, verdict, confidence=0.8, problem_type=None):
    if verdict == "problem" and problem_type is None:
        problem_type = {"route-corridor": "off_route", "heading-speed": "wrong_direction",
                        "dwell-time": "long_dwell", "ml-anomaly": "off_route"}[detector]
    return {"detectorId": detector, "verdict": verdict, "confidence": confidence, "problemType": problem_type}


# --- the reported bug: specialists outvoted by detectors that don't check that problem ----------

def test_off_route_is_not_outvoted_by_detectors_that_dont_check_the_route():
    # Traveller km off the route: route-corridor sees it; heading/dwell check other things.
    d = aggregate([v(ROUTE, "problem", 0.8), v(DWELL, "normal", 0.8),
                   v(HEADING, "normal", 0.7), v(ML, "abstain", 0.0)])
    assert d.decision == "problem"
    assert d.problem_type == "off_route"


def test_first_pair_asks_for_a_second_opinion_on_off_route():
    # k=2 (route + dwell): dwell can't judge off_route, ml can and hasn't been asked.
    d = aggregate([v(ROUTE, "problem", 0.8), v(DWELL, "normal", 0.8)])
    assert d.decision == "inconclusive"
    assert d.reason == "single_voter"
    assert d.needs_more_votes


def test_each_specialist_is_heard_for_its_own_problem():
    d = aggregate([v(ROUTE, "normal"), v(DWELL, "problem", 0.7), v(HEADING, "normal"), v(ML, "abstain", 0)])
    assert d.decision == "problem" and d.problem_type == "long_dwell"
    d = aggregate([v(ROUTE, "normal"), v(DWELL, "normal"), v(HEADING, "problem", 0.7), v(ML, "abstain", 0)])
    assert d.decision == "problem" and d.problem_type == "wrong_direction"


def test_checks():
    assert checks(ROUTE, "off_route") and not checks(ROUTE, "long_dwell")
    assert checks(ML, "long_dwell") and checks(ML, "off_route")
    assert checks("some-new-detector", "off_route")      # unknown detectors are generalists
    assert checks(DWELL, "unknown")                       # a typeless problem is judged by everyone


# --- basic outcomes -------------------------------------------------------------------------------

def test_both_say_problem():
    d = aggregate([v(ROUTE, "problem", 0.9), v(ML, "problem", 0.7)])
    assert d.decision == "problem"
    assert d.problem_type == "off_route"
    assert d.confidence == pytest.approx(1.0)
    assert d.counted == 2


def test_all_say_normal():
    d = aggregate([v(ROUTE, "normal", 0.9), v(DWELL, "normal", 0.6)])
    assert d.decision == "normal"
    assert d.problem_type is None
    assert d.confidence == pytest.approx(1.0)
    assert d.problem_score == pytest.approx(0.0)


def test_confident_problem_beats_weak_normal_within_the_jury():
    d = aggregate([v(ROUTE, "problem", 0.9), v(ML, "normal", 0.3), v(DWELL, "normal"), v(HEADING, "normal")])
    assert d.decision == "problem"
    assert d.problem_score == pytest.approx(0.9 / 1.2)


def test_confident_normal_beats_weak_problem_within_the_jury():
    d = aggregate([v(ROUTE, "problem", 0.2), v(ML, "normal", 0.9), v(DWELL, "normal"), v(HEADING, "normal")])
    assert d.decision == "normal"
    assert d.problem_score == pytest.approx(0.2 / 1.1)
    assert d.confidence == pytest.approx(0.9 / 1.1)


# --- close calls: ask more detectors instead of guessing ---------------------------------------------

def test_tie_between_the_only_two_judges_counts_as_problem():
    # Only route-corridor and ml-anomaly check off_route. heading/dwell aren't asked yet, but they
    # can't judge it, so there's nobody to break the tie: it counts as a problem (safety first).
    d = aggregate([v(ROUTE, "problem", 0.8), v(ML, "normal", 0.8)])
    assert d.decision == "problem"
    assert d.problem_score == pytest.approx(0.5)


def test_close_call_asks_more_while_a_judge_is_left():
    # A typeless problem is judged by every detector, so the unasked two are worth asking.
    d = aggregate([v(ROUTE, "problem", 0.8, "unknown"), v(DWELL, "normal", 0.8)])
    assert d.decision == "inconclusive"
    assert d.reason == "split"
    assert d.needs_more_votes
    assert d.problem_score == pytest.approx(0.5)   # kept, for the dashboard


def test_score_at_edge_of_close_band_is_still_split():
    d = aggregate([v(ROUTE, "problem", 0.6, "unknown"), v(DWELL, "normal", 0.4)])
    assert d.reason == "split"


def test_score_outside_close_band_is_decided():
    d = aggregate([v(ROUTE, "problem", 0.7, "unknown"), v(DWELL, "normal", 0.3)])
    assert d.decision == "problem"
    assert not d.needs_more_votes


def test_two_vs_two_tie_with_all_four_counts_as_problem():
    d = aggregate([v(ROUTE, "problem", 0.8, "unknown"), v(HEADING, "problem", 0.8, "unknown"),
                   v(DWELL, "normal", 0.8), v(ML, "normal", 0.8)])
    assert d.decision == "problem"
    assert d.problem_score == pytest.approx(0.5)
    assert not d.needs_more_votes


def test_close_call_with_all_four_is_decided_by_the_threshold():
    d = aggregate([v(ROUTE, "problem", 0.5, "unknown"), v(HEADING, "problem", 0.4, "unknown"),
                   v(DWELL, "normal", 0.6), v(ML, "normal", 0.5)])   # 0.9 / 2.0 = 0.45
    assert d.decision == "normal"
    assert not d.needs_more_votes


def test_zero_margin_only_escalates_exact_ties():
    near = [v(ROUTE, "problem", 0.81, "unknown"), v(DWELL, "normal", 0.79)]
    assert aggregate(near, close_margin=0).decision == "problem"
    exact = [v(ROUTE, "problem", 0.8, "unknown"), v(DWELL, "normal", 0.8)]
    assert aggregate(exact, close_margin=0).reason == "split"


def test_higher_threshold_turns_a_tie_into_normal():
    assert aggregate([v(ROUTE, "problem", 0.8), v(ML, "normal", 0.8)], threshold=0.6).decision == "normal"


# --- abstains and quorum -------------------------------------------------------------------------------

def test_abstains_are_ignored():
    d = aggregate([v(ROUTE, "problem", 0.9), v(DWELL, "abstain", 0.0), v(ML, "problem", 0.6)])
    assert d.decision == "problem"
    assert d.counted == 2


def test_one_real_vote_is_inconclusive():
    d = aggregate([v(ROUTE, "problem", 0.9), v(DWELL, "abstain", 0.0)])
    assert d.decision == "inconclusive"
    assert d.reason == "quorum"
    assert d.needs_more_votes
    assert d.problem_score is None
    assert d.counted == 1


def test_all_abstain_is_inconclusive():
    assert aggregate([v(ROUTE, "abstain"), v(DWELL, "abstain")]).decision == "inconclusive"


def test_too_few_votes_after_asking_all_four_is_final():
    d = aggregate([v(ROUTE, "problem", 0.9), v(DWELL, "abstain"), v(HEADING, "abstain"), v(ML, "abstain")])
    assert d.decision == "inconclusive"
    assert not d.needs_more_votes   # nobody left to ask


def test_no_votes_is_inconclusive():
    assert aggregate([]).decision == "inconclusive"


def test_unknown_verdict_is_treated_like_an_abstain():
    assert aggregate([v(ROUTE, "maybe"), v(DWELL, "normal")]).decision == "inconclusive"


def test_all_zero_confidence_is_inconclusive():
    d = aggregate([v(ROUTE, "problem", 0.0), v(DWELL, "normal", 0.0)])
    assert d.decision == "inconclusive"
    assert d.reason == "no_weight"


def test_quorum_of_one_with_nobody_else_to_ask():
    d = aggregate([v(ROUTE, "problem", 0.9)], quorum=1, all_detectors=(ROUTE,))
    assert d.decision == "problem"


def test_quorum_of_one_still_seeks_a_second_judge():
    d = aggregate([v(ROUTE, "problem", 0.9)], quorum=1)
    assert d.reason == "single_voter" and d.needs_more_votes


# --- reliability weights (detector:weights) and faulty detectors -----------------------------------------

def test_unreliable_detector_is_outvoted_within_the_jury():
    # ml-anomaly is known to be wrong (Member 4's fault mode); its weight is lowered.
    votes = [v(ROUTE, "problem", 0.5), v(ML, "normal", 0.9), v(DWELL, "normal"), v(HEADING, "normal")]
    assert aggregate(votes).decision == "normal"                               # 0.5 / 1.4 = 0.36
    assert aggregate(votes, reliability={ML: 0.3}).decision == "problem"       # 0.5 / 0.77 = 0.65


def test_one_wrong_detector_out_of_four_is_outvoted():
    # Three detectors report the same problem; the faulty one says normal at full confidence.
    votes = [v(ROUTE, "problem", 0.8, "unknown"), v(HEADING, "problem", 0.7, "unknown"),
             v(ML, "problem", 0.6, "unknown"), v(DWELL, "normal", 1.0)]
    d = aggregate(votes)
    assert d.decision == "problem"
    assert d.counted == 4


def test_missing_reliability_defaults_to_one():
    assert vote_weight(v(ROUTE, "problem", 0.8), {}) == pytest.approx(0.8)


def test_negative_reliability_counts_as_zero():
    assert vote_weight(v(ROUTE, "problem", 0.8), {ROUTE: -1}) == 0.0


def test_confidence_outside_0_1_is_clamped():
    assert vote_weight(v(ROUTE, "problem", 1.7), {}) == 1.0
    assert vote_weight(v(ROUTE, "problem", -0.5), {}) == 0.0


def test_missing_or_bad_confidence_counts_as_zero():
    assert vote_weight({"detectorId": ROUTE, "verdict": "problem"}, {}) == 0.0
    assert vote_weight({"detectorId": ROUTE, "verdict": "problem", "confidence": "high"}, {}) == 0.0


# --- several problems at once --------------------------------------------------------------------------

def test_strongest_confirmed_problem_is_reported():
    d = aggregate([v(ROUTE, "problem", 0.6), v(HEADING, "problem", 0.9), v(DWELL, "normal"), v(ML, "abstain", 0)])
    assert d.decision == "problem"
    assert d.problem_type in ("off_route", "wrong_direction")
    assert d.problem_score == pytest.approx(1.0)


def test_equal_problems_are_reported_alphabetically():
    d = aggregate([v(ROUTE, "problem", 0.5), v(HEADING, "problem", 0.5), v(DWELL, "normal"), v(ML, "abstain", 0)])
    assert d.problem_type == "off_route"


def test_a_problem_of_another_type_does_not_count_against():
    # ml says wrong_direction, route says off_route: both stand, neither cancels the other.
    d = aggregate([v(ROUTE, "problem", 0.8), v(ML, "problem", 0.6, "wrong_direction"),
                   v(HEADING, "normal", 0.9), v(DWELL, "normal")])
    assert d.decision == "problem" and d.problem_type == "off_route"


def test_problem_without_a_type_is_judged_by_everyone():
    d = aggregate([v(ROUTE, "problem", 0.9, "unknown"), v(DWELL, "problem", 0.9, "unknown")])
    assert d.decision == "problem"
    assert d.problem_type is None


def test_normal_reports_the_highest_rejected_score():
    d = aggregate([v(ROUTE, "problem", 0.3), v(ML, "normal", 0.7), v(DWELL, "normal"), v(HEADING, "normal")])
    assert d.decision == "normal"
    assert d.problem_score == pytest.approx(0.3)   # feeds the risk score: a shaky normal


# --- contract examples ------------------------------------------------------------------------------------

def test_votes_from_the_contract_example():
    report = json.loads((CONTRACTS / "problem-report.json").read_text())
    votes = [{**x, "problemType": "off_route"} if x["verdict"] == "problem" else x for x in report["votes"]]
    d = aggregate(votes)
    assert d.decision == "problem" and d.problem_type == "off_route"
    assert d.counted == 4
    # dwell-time's "normal" says nothing about the route, so all of the off_route jury agrees.
    assert d.problem_score == pytest.approx(1.0)


def test_detector_vote_example_is_counted():
    vote = json.loads((CONTRACTS / "detector-vote.json").read_text())   # route-corridor, off_route
    d = aggregate([vote, {**vote, "detectorId": ML}])
    assert d.decision == "problem"
    assert d.problem_type == vote["problemType"]
