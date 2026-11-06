"""The scoring definitions in analyse.py, on small made-up journeys (no stack needed)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyse import answer, c1, score_journey  # noqa: E402

DETECTORS = ("route-corridor", "heading-speed", "dwell-time", "route-progress")


def vote(detector, verdict="normal", ptype=None):
    return {"detectorId": detector, "verdict": verdict, "problemType": ptype}


def row(i, expected, decision="normal", ptype=None, votes=None):
    """Check-in i (10 s apart). votes: {detector: (verdict, problemType)}; the rest say normal."""
    votes = votes or {}
    return {"seq": i, "timestamp": f"2026-10-12T08:{i // 6:02d}:{i % 6 * 10:02d}Z", "expected": expected,
            "endToEndMs": 40.0,
            "decision": {"k": 4, "decision": decision, "problemType": ptype,
                         "votes": [vote(d, *votes.get(d, ("normal",))) for d in DETECTORS]}}


def off_route_journey():
    """2 normal check-ins, then 4 labelled off_route. route-corridor catches it on the 3rd
    labelled one; route-progress never does; the Coordinator follows route-corridor."""
    hit = {"route-corridor": ("problem", "off_route")}
    return {"scenario": "off_route", "rows": [
        row(0, "normal"), row(1, "normal"),
        row(2, "off_route"), row(3, "off_route"),
        row(4, "off_route", "problem", "off_route", hit), row(5, "off_route", "problem", "off_route", hit)]}


def test_detected_and_delay_from_the_first_labelled_checkin():
    s = score_journey(off_route_journey(), "combined")
    assert s["problem"] == "off_route" and s["detected"] is True
    assert s["delayS"] == 20.0 and s["falseAlarms"] == 0


def test_a_detector_that_never_says_it_is_a_miss():
    s = score_journey(off_route_journey(), "route-progress")
    assert s["detected"] is False and s["delayS"] is None


def test_a_detector_is_not_scored_on_problems_it_does_not_check():
    s = score_journey(off_route_journey(), "dwell-time")
    assert "detected" not in s


def test_problem_on_a_normal_checkin_is_a_false_alarm():
    j = {"scenario": "normal", "rows": [row(0, "normal"),
                                        row(1, "normal", "problem", "long_dwell", {"dwell-time": ("problem", "long_dwell")})]}
    assert score_journey(j, "combined")["falseAlarms"] == 1
    assert score_journey(j, "dwell-time")["falseAlarms"] == 1
    assert score_journey(j, "route-corridor")["falseAlarms"] == 0


def test_problem_of_the_wrong_type_during_a_real_problem_is_wrong_type_not_a_false_alarm():
    j = {"scenario": "off_route", "rows": [row(0, "off_route", "problem", "wrong_direction",
                                               {"heading-speed": ("problem", "wrong_direction")})]}
    s = score_journey(j, "combined")
    assert s["wrongType"] == 1 and s["falseAlarms"] == 0 and s["detected"] is False


def test_the_coordinator_is_judged_on_every_type_it_reported():
    r = row(0, "off_route", "problem", "wrong_direction",
            {"heading-speed": ("problem", "wrong_direction"), "route-progress": ("problem", "off_route")})
    r["decision"]["alerts"] = [{"problemType": "wrong_direction"}, {"problemType": "off_route"}]
    s = score_journey({"scenario": "off_route", "rows": [r]}, "combined")
    assert s["detected"] is True and s["wrongType"] == 0


def test_an_alert_of_another_type_counts_for_the_first_alert_not_for_detected():
    wrong = {"heading-speed": ("problem", "wrong_direction")}
    right = {"route-corridor": ("problem", "off_route")}
    j = {"scenario": "off_route", "rows": [row(0, "off_route", "problem", "wrong_direction", wrong),
                                           row(1, "off_route", "problem", "off_route", right)]}
    s = score_journey(j, "combined")
    assert s["anyDelayS"] == 0.0 and s["delayS"] == 10.0 and s["detected"] is True


def test_poor_gps_must_not_alert_and_abstains_are_no_answer():
    j = {"scenario": "poor_gps", "rows": [row(0, "abstain", "inconclusive", None,
                                              {d: ("abstain", None) for d in DETECTORS})]}
    s = score_journey(j, "combined")
    assert s["falseAlarms"] == 0 and s["noAnswer"] == 1
    assert answer(j["rows"][0], "route-corridor") == ("none", set())


def test_an_undecided_checkin_is_no_answer_for_everyone():
    r = {**row(0, "normal"), "decision": None, "endToEndMs": None}
    assert answer(r, "combined") == ("none", set()) and answer(r, "heading-speed") == ("none", set())


def test_summary_counts_alerts_from_the_alert_service():
    journeys = [{**off_route_journey(), "incidents": [{"problemType": "off_route"}]},
                {"scenario": "normal", "rows": [row(0, "normal")], "incidents": [{"problemType": "long_dwell"}]}]
    s = c1({"runId": "t", "seeds": 1, "journeys": journeys})
    assert s["alerts"] == {"withoutProblem": {"journeys": 1, "falseAlerts": 1},
                           "cases": {"off_route": {"journeys": 1, "rightType": 1, "anyAlert": 1}}}
    assert s["judges"]["combined"]["problems"]["off_route"] == {"journeys": 1, "detected": 1, "medianDelayS": 20.0,
                                                                "medianAnyDelayS": 20.0}
    assert s["judges"]["route-progress"]["problems"]["off_route"]["detected"] == 0
    assert "off_route" not in s["judges"]["dwell-time"]["problems"]


def test_near_leg_journeys_are_scored_as_their_own_case():
    near = {**off_route_journey(), "case": "off_route_near",
            "incidents": [{"problemType": "wrong_direction"}]}   # alerted, but as another type
    s = c1({"runId": "t", "seeds": 1, "journeys": [near]})
    assert set(s["judges"]["combined"]["problems"]) == {"off_route_near"}
    assert s["judges"]["route-corridor"]["problems"]["off_route_near"]["detected"] == 1
    assert s["alerts"]["cases"] == {"off_route_near": {"journeys": 1, "rightType": 0, "anyAlert": 1}}
