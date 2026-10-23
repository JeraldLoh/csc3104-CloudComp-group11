"""Contract tests: the shared JSON examples must round-trip through our models.

If someone changes a file in contracts/examples in a way that breaks the
detectors, these tests fail.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from detectors.models import CheckIn, DetectorRequest, DetectorVote

EXAMPLES = Path(__file__).resolve().parents[3] / "contracts" / "examples"


def load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text())


def dump(model) -> dict:
    return model.model_dump(by_alias=True, mode="json")


def test_checkin_example_round_trips():
    raw = load("checkin.json")
    assert dump(CheckIn.model_validate(raw)) == raw


def test_checkin_batch_entries_parse_with_null_speed_and_heading():
    batch = load("checkin-batch.json")
    for entry in batch["checkins"]:
        checkin = CheckIn.model_validate(
            {**entry, "tripId": batch["tripId"], "travellerId": batch["travellerId"]}
        )
        assert checkin.speed_mps is None
        assert checkin.heading_deg is None
        assert checkin.buffered_offline is True


def test_detector_request_example_round_trips():
    raw = load("detector-request.json")
    # The request's check-in has no bufferedOffline; we default it to False.
    expected = {**raw, "checkin": {**raw["checkin"], "bufferedOffline": False}}
    assert dump(DetectorRequest.model_validate(raw)) == expected


def test_detector_vote_example_round_trips():
    raw = load("detector-vote.json")
    assert dump(DetectorVote.model_validate(raw)) == raw


def test_problem_report_vote_verdicts_are_known_values():
    # problem-report.json carries a trimmed vote list; make sure every verdict
    # and detectorId there is one our model accepts.
    for vote in load("problem-report.json")["votes"]:
        DetectorVote.model_validate(
            {
                **vote,
                "requestId": "T-001#17",
                "reason": "",
                "problemType": "off_route" if vote["verdict"] == "problem" else None,
            }
        )


def test_timestamps_serialise_as_utc_with_z():
    raw = {**load("checkin.json"), "timestamp": "2026-10-12T16:15:30+08:00"}
    out = dump(CheckIn.model_validate(raw))
    assert out["timestamp"] == "2026-10-12T08:15:30Z"


def test_naive_timestamp_rejected():
    raw = {**load("checkin.json"), "timestamp": "2026-10-12T08:15:30"}
    with pytest.raises(ValidationError):
        CheckIn.model_validate(raw)


def test_unknown_extra_fields_are_ignored():
    raw = {**load("checkin.json"), "someFutureField": 123}
    assert "someFutureField" not in dump(CheckIn.model_validate(raw))


def test_populate_by_snake_case_name():
    checkin = CheckIn(
        trip_id="T-1",
        traveller_id="U-1",
        seq=0,
        timestamp=datetime(2026, 10, 12, tzinfo=timezone.utc),
        lat=1.3,
        lng=103.8,
        step_index=0,
        route_version=1,
        status="active",
    )
    assert dump(checkin)["tripId"] == "T-1"


def _vote(**overrides) -> dict:
    return {**load("detector-vote.json"), **overrides}


def test_abstain_helper():
    vote = DetectorVote.abstain("T-001#17", "dwell-time", "GPS accuracy 85 m exceeds 50 m limit")
    out = dump(vote)
    assert out["verdict"] == "abstain"
    assert out["problemType"] is None
    assert out["confidence"] == 0
    assert out["detectorId"] == "dwell-time"


def test_normal_vote_has_null_problem_type():
    vote = DetectorVote.model_validate(_vote(verdict="normal", problemType=None, confidence=0.6))
    assert vote.problem_type is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"verdict": "problem", "problemType": None},  # problem needs a type
        {"verdict": "normal"},  # normal must not carry a type
        {"verdict": "abstain", "problemType": None, "confidence": 0.4},  # abstain -> 0
        {"verdict": "unknown"},  # not a contract value
        {"detectorId": "detector-route"},  # that's the service name, not detectorId
        {"problemType": "lost"},  # not a contract problem type
        {"confidence": 1.2},
        {"latencyMs": -1},
    ],
)
def test_invalid_votes_rejected(overrides):
    with pytest.raises(ValidationError):
        DetectorVote.model_validate(_vote(**overrides))
