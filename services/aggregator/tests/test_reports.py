import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.reports import UNKNOWN_PROBLEM, build_problem_report, iso_z
from app.voting import Decision, aggregate

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "examples"
NOW = datetime(2026, 10, 12, 8, 15, 31, tzinfo=timezone.utc)


def example(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text())


@pytest.fixture
def contract_case():
    """The checkin and votes behind contracts/examples/problem-report.json.
    The report keeps only detectorId/verdict/confidence per vote; the detectors' own votes
    also carry problemType, so it's added back for the "problem" ones."""
    expected = example("problem-report.json")
    checkin = example("checkin.json")
    votes = [{**v, "problemType": "off_route"} if v["verdict"] == "problem" else dict(v)
             for v in expected["votes"]]
    return expected, checkin, votes, aggregate(votes)


# --- matches the contract ------------------------------------------------------------

def test_has_exactly_the_contract_fields(contract_case):
    expected, checkin, votes, decision = contract_case
    report = build_problem_report("T-001#17", checkin, votes, decision, NOW)
    assert set(report) == set(expected)
    assert set(report["location"]) == set(expected["location"])
    assert all(set(v) == set(expected["votes"][0]) for v in report["votes"])


def test_values_match_the_contract_example(contract_case):
    expected, checkin, votes, decision = contract_case
    report = build_problem_report("T-001#17", checkin, votes, decision, NOW)
    for field in ("reportId", "source", "travellerId", "tripId", "stepIndex", "problemType",
                  "detectorsUsed", "votes", "location", "batteryPct", "timestamp"):
        assert report[field] == expected[field], field
    # The example's 0.81 is illustrative; ours is computed from its votes.
    assert report["confidence"] == round(2.33 / 2.93, 2)


def test_is_json_serialisable(contract_case):
    _, checkin, votes, decision = contract_case
    json.dumps(build_problem_report("T-001#17", checkin, votes, decision, NOW))


# --- details --------------------------------------------------------------------------

def test_detectors_used_counts_abstains_too():
    checkin = example("checkin.json")
    votes = [
        {"detectorId": "route-corridor", "verdict": "problem", "confidence": 0.9, "problemType": "off_route"},
        {"detectorId": "dwell-time", "verdict": "abstain", "confidence": 0.0},
        {"detectorId": "heading-speed", "verdict": "problem", "confidence": 0.8, "problemType": "off_route"},
        {"detectorId": "ml-anomaly", "verdict": "problem", "confidence": 0.7, "problemType": "off_route"},
    ]
    report = build_problem_report("T-001#17", checkin, votes, aggregate(votes), NOW)
    assert report["detectorsUsed"] == 4
    assert report["votes"][1] == {"detectorId": "dwell-time", "verdict": "abstain", "confidence": 0.0}


def test_vote_extras_are_left_out():
    vote = example("detector-vote.json")   # has reason, latencyMs, requestId, problemType
    votes = [vote, {**vote, "detectorId": "dwell-time"}]
    report = build_problem_report("T-001#17", example("checkin.json"), votes, aggregate(votes), NOW)
    assert report["votes"][0] == {"detectorId": "route-corridor", "verdict": "problem", "confidence": 0.85}


def test_missing_problem_type_falls_back_to_unknown():
    d = Decision("problem", None, 0.9, 0.9, 2)
    report = build_problem_report("T-001#17", example("checkin.json"), [], d, NOW)
    assert report["problemType"] == UNKNOWN_PROBLEM


def test_unknown_step_and_battery_are_null():
    checkin = {**example("checkin.json"), "stepIndex": None}
    del checkin["batteryPct"]
    d = Decision("problem", "off_route", 0.9, 0.9, 2)
    report = build_problem_report("T-001#17", checkin, [], d, NOW)
    assert report["stepIndex"] is None
    assert report["batteryPct"] is None


@pytest.mark.parametrize("battery, expected", [
    (64, 64), (64.0, 64), (63.6, 64), (63.4, 63), ("57", 57), (None, None), ("low", None),
])
def test_battery_is_sent_as_a_whole_number(battery, expected):
    # The Alert Service (Member 5) only accepts an int; 63.5 would be rejected and the alert lost.
    d = Decision("problem", "off_route", 0.9, 0.9, 2)
    report = build_problem_report("T-001#17", {**example("checkin.json"), "batteryPct": battery}, [], d, NOW)
    assert report["batteryPct"] == expected
    assert report["batteryPct"] is None or type(report["batteryPct"]) is int


def test_confidence_is_rounded():
    d = Decision("problem", "off_route", 0.876543, 0.876543, 2)
    assert build_problem_report("T-001#17", example("checkin.json"), [], d, NOW)["confidence"] == 0.88


@pytest.mark.parametrize("decision", [
    Decision("normal", None, 0.9, 0.1, 2),
    Decision("inconclusive", None, 0.0, None, 1, "quorum"),
])
def test_only_problems_are_reported(decision):
    with pytest.raises(ValueError):
        build_problem_report("T-001#17", example("checkin.json"), [], decision, NOW)


# --- time format --------------------------------------------------------------------------

def test_timestamp_is_utc_with_z():
    sgt = timezone(timedelta(hours=8))
    assert iso_z(datetime(2026, 10, 12, 16, 15, 31, 999000, tzinfo=sgt)) == "2026-10-12T08:15:31Z"


def test_timestamp_defaults_to_now():
    d = Decision("problem", "off_route", 0.9, 0.9, 2)
    report = build_problem_report("T-001#17", example("checkin.json"), [], d)
    sent = datetime.strptime(report["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs((datetime.now(timezone.utc) - sent).total_seconds()) < 5
