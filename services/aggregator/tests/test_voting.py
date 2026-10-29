import json
from pathlib import Path

import pytest

from app.voting import ALL_DETECTORS, COVERAGE, aggregate, checks, vote_weight

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "examples"

# Who checks what (Member 4's detectors): route -> off_route, heading -> wrong_direction,
# dwell -> long_dwell, progress -> off_route/wrong_direction/missed_stop.
ROUTE, HEADING, DWELL, PROGRESS = ALL_DETECTORS
OWN_TYPE = {ROUTE: "off_route", HEADING: "wrong_direction", DWELL: "long_dwell", PROGRESS: "off_route"}


def v(detector, verdict, confidence=0.8, problem_type=None):
    if verdict == "problem" and problem_type is None:
        problem_type = OWN_TYPE[detector]
    return {"detectorId": detector, "verdict": verdict, "confidence": confidence, "problemType": problem_type}


def asked_all(*votes, rest="abstain"):
    """The given votes, plus `rest` from every detector not mentioned (so nobody is left to ask)."""
    named = {x["detectorId"] for x in votes}
    return list(votes) + [v(d, rest, 0.0 if rest == "abstain" else 0.8) for d in ALL_DETECTORS if d not in named]


# --- coverage ----------------------------------------------------------------------------------------

def test_coverage_matches_the_detectors():
    assert set(COVERAGE) == set(ALL_DETECTORS)


def test_checks():
    assert checks(ROUTE, "off_route") and not checks(ROUTE, "long_dwell")
    assert checks(PROGRESS, "off_route") and checks(PROGRESS, "wrong_direction") and checks(PROGRESS, "missed_stop")
    assert not checks(PROGRESS, "long_dwell") and not checks(HEADING, "off_route")
    assert checks("some-new-detector", "off_route")      # unknown detectors are generalists
    assert checks(DWELL, "unknown")                       # a typeless problem is judged by everyone


# --- rule 1: one credible problem claim is enough --------------------------------------------------------

def test_off_route_is_not_cancelled_by_detectors_that_dont_check_the_route():
    # Check-in #18 from the demo. Counting all four, "normal" outweighed "problem" (1.76 vs 1.56)
    # and the off-route traveller was missed. heading-speed and dwell-time don't check the route.
    d = aggregate([v(ROUTE, "problem", 0.84), v(PROGRESS, "problem", 0.72),
                   v(HEADING, "normal", 0.82), v(DWELL, "normal", 0.94)])
    assert d.decision == "problem" and d.problem_type == "off_route"


@pytest.mark.parametrize("route, progress", [("problem", "normal"), ("normal", "problem"), ("problem", "abstain")])
def test_off_route_alerts_if_either_route_judge_says_so(route, progress):
    # Safety first: the judges disagreeing (or one not answering) still raises the alert.
    d = aggregate([v(ROUTE, route, 0.84), v(PROGRESS, progress, 0.95, "off_route" if progress == "problem" else None),
                   v(HEADING, "normal"), v(DWELL, "normal")])
    assert d.decision == "problem" and d.problem_type == "off_route"


def test_a_confident_normal_does_not_cancel_a_problem():
    d = aggregate([v(ROUTE, "problem", 0.5), v(PROGRESS, "normal", 0.95)])
    assert d.decision == "problem"


def test_each_specialist_is_heard_for_its_own_problem():
    d = aggregate(asked_all(v(ROUTE, "normal"), v(DWELL, "problem", 0.7), v(HEADING, "normal")))
    assert d.decision == "problem" and d.problem_type == "long_dwell"
    d = aggregate(asked_all(v(ROUTE, "normal"), v(DWELL, "normal"), v(HEADING, "problem", 0.7)))
    assert d.decision == "problem" and d.problem_type == "wrong_direction"


def test_route_progress_catches_walking_backwards_near_the_route():
    # Within 100 m of the path (route-corridor fine), but going backwards along it.
    d = aggregate(asked_all(v(ROUTE, "normal", 0.9), v(HEADING, "abstain", 0), v(PROGRESS, "problem", 0.8, "wrong_direction")))
    assert d.decision == "problem" and d.problem_type == "wrong_direction"


def test_missed_stop_is_judged_by_route_progress():
    # On the bus past the alighting stop: still on the bus's path, so route-corridor says normal.
    d = aggregate(asked_all(v(ROUTE, "normal", 0.9), v(PROGRESS, "problem", 0.8, "missed_stop"), v(DWELL, "normal")))
    assert d.decision == "problem" and d.problem_type == "missed_stop"


def test_long_dwell_has_a_single_judge():
    d = aggregate([v(ROUTE, "normal"), v(DWELL, "problem", 0.7)])
    assert d.decision == "problem" and d.problem_type == "long_dwell"


def test_one_problem_vote_is_enough_when_the_others_abstain():
    # The quorum is for calling it normal; a problem is reported even if only one detector answered.
    d = aggregate(asked_all(v(ROUTE, "problem", 0.9)))
    assert d.decision == "problem" and d.counted == 1


def test_adaptive_pair_alerts_at_once():
    # k=2 (route + dwell): no second opinion is needed before alerting.
    d = aggregate([v(ROUTE, "problem", 0.8), v(DWELL, "normal", 0.8)])
    assert d.decision == "problem" and not d.needs_more_votes


def test_problem_outside_a_detectors_coverage_is_ignored():
    # heading-speed doesn't check the route, so its off_route claim isn't credible.
    d = aggregate([v(HEADING, "problem", 0.9, "off_route"), v(ROUTE, "normal"), v(DWELL, "normal")])
    assert d.decision == "normal"


def test_problem_without_a_type_counts_from_anyone():
    d = aggregate([v(DWELL, "problem", 0.9, "unknown"), v(ROUTE, "normal")])
    assert d.decision == "problem"
    assert d.problem_type is None


def test_zero_reliability_silences_a_faulty_detector():
    # route-corridor known to be faulty: its reliability is set to 0 in detector:weights.
    votes = [v(ROUTE, "problem", 0.9), v(PROGRESS, "normal"), v(HEADING, "normal"), v(DWELL, "normal")]
    assert aggregate(votes).decision == "problem"
    assert aggregate(votes, reliability={ROUTE: 0}).decision == "normal"


def test_confidence_is_the_strongest_claim():
    d = aggregate([v(ROUTE, "problem", 0.84), v(PROGRESS, "problem", 0.72)], reliability={ROUTE: 0.5})
    assert d.confidence == pytest.approx(0.72)     # 0.84 x 0.5 = 0.42 < 0.72
    assert d.problem_score == pytest.approx(0.72)


def test_strongest_problem_is_reported():
    d = aggregate(asked_all(v(ROUTE, "problem", 0.6), v(HEADING, "problem", 0.9), v(DWELL, "normal")))
    assert d.problem_type == "wrong_direction"


def test_equal_problems_are_reported_alphabetically():
    d = aggregate(asked_all(v(ROUTE, "problem", 0.5), v(HEADING, "problem", 0.5), v(DWELL, "normal")))
    assert d.problem_type == "off_route"


# --- rules 2 and 3: normal needs enough answers -------------------------------------------------------------

def test_all_say_normal():
    d = aggregate([v(ROUTE, "normal", 0.9), v(DWELL, "normal", 0.6)])
    assert d.decision == "normal"
    assert d.problem_type is None
    assert d.confidence == pytest.approx(1.0)
    assert d.problem_score == pytest.approx(0.0)


def test_one_normal_vote_is_inconclusive_and_asks_the_rest():
    d = aggregate([v(ROUTE, "normal", 0.9), v(DWELL, "abstain", 0.0)])
    assert d.decision == "inconclusive"
    assert d.reason == "quorum"
    assert d.needs_more_votes
    assert d.problem_score is None
    assert d.counted == 1


def test_too_few_votes_after_asking_everyone_is_final():
    d = aggregate(asked_all(v(ROUTE, "normal", 0.9)))
    assert d.decision == "inconclusive"
    assert not d.needs_more_votes   # nobody left to ask


def test_all_abstain_is_inconclusive():
    assert aggregate([v(ROUTE, "abstain"), v(DWELL, "abstain")]).decision == "inconclusive"


def test_no_votes_is_inconclusive():
    assert aggregate([]).decision == "inconclusive"


def test_unknown_verdict_is_treated_like_an_abstain():
    assert aggregate([v(ROUTE, "maybe"), v(DWELL, "normal")]).decision == "inconclusive"


def test_all_zero_weight_is_inconclusive():
    d = aggregate([v(ROUTE, "problem", 0.0), v(DWELL, "normal", 0.0)])
    assert d.decision == "inconclusive"
    assert d.reason == "no_weight"


def test_quorum_of_one():
    assert aggregate([v(ROUTE, "normal", 0.9)], quorum=1).decision == "normal"


# --- reliability weights (detector:weights) ------------------------------------------------------------------

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


# --- contract examples ---------------------------------------------------------------------------------------

def test_votes_from_the_contract_example():
    report = json.loads((CONTRACTS / "problem-report.json").read_text())
    votes = [{**x, "problemType": "off_route"} if x["verdict"] == "problem" else x for x in report["votes"]]
    d = aggregate(asked_all(*votes))
    assert d.decision == "problem" and d.problem_type == "off_route"
    assert d.counted == 4
    assert d.problem_score == pytest.approx(max(x["confidence"] for x in votes if x["verdict"] == "problem"))


def test_detector_vote_example_is_counted():
    vote = json.loads((CONTRACTS / "detector-vote.json").read_text())   # route-corridor, off_route
    d = aggregate([vote, {**vote, "detectorId": PROGRESS}])
    assert d.decision == "problem"
    assert d.problem_type == vote["problemType"]
