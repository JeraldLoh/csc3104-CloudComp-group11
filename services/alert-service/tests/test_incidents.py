"""IncidentStore against LocalStack: one incident per problem, and the open/acknowledged/resolved states."""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app.errors import ApiError
from app.incidents import incident_id
from app.models import ProblemReport


def iid_of(report: dict) -> str:
    return incident_id(report["travellerId"], report["tripId"], report["problemType"], report["stepIndex"])


# ---------------------------------------------------------------------------- one incident per problem
def test_first_report_creates_an_open_incident(store, report):
    incident, outcome = store.record_problem(ProblemReport(**report))

    assert outcome == "created"
    assert incident["incidentId"] == f"{report['travellerId']}#T-001#off_route#2"
    assert incident["state"] == "open"
    assert incident["severity"] == "normal"
    assert incident["detectionCount"] == 1
    assert incident["detectedAt"] == "2026-10-12T08:15:31Z"
    assert store.get(incident["incidentId"])["lastLocation"] == {"lat": 1.4052, "lng": 103.9024}


def test_same_problem_again_updates_the_same_incident(store, report):
    store.record_problem(ProblemReport(**report))
    incident, outcome = store.record_problem(ProblemReport(**report))

    assert outcome == "updated"
    assert incident["detectionCount"] == 2
    assert len(store.list_for_traveller(report["travellerId"])) == 1


def test_concurrent_reports_of_one_problem_create_exactly_one_incident(store, report):
    """20 replicas handle the same problem at the same moment: one creates it, 19 update it."""
    copies = 20
    start = threading.Barrier(copies)

    def send(_):
        start.wait()  # release all threads together, to make the race as tight as possible
        return store.record_problem(ProblemReport(**report))[1]

    with ThreadPoolExecutor(copies) as pool:
        outcomes = list(pool.map(send, range(copies)))

    assert outcomes.count("created") == 1
    assert outcomes.count("updated") == copies - 1
    assert store.get(iid_of(report))["detectionCount"] == copies
    assert len(store.list_for_traveller(report["travellerId"])) == 1


def test_different_step_is_a_different_incident(store, report):
    store.record_problem(ProblemReport(**report))
    later_step = {**report, "stepIndex": 3}
    _, outcome = store.record_problem(ProblemReport(**later_step))

    assert outcome == "created"
    assert len(store.list_for_traveller(report["travellerId"])) == 2


def test_no_response_from_heartbeat_is_urgent(store, no_response_report):
    incident, _ = store.record_problem(ProblemReport(**no_response_report))

    assert incident["severity"] == "urgent"
    assert incident["confidence"] is None
    assert incident["source"] == "heartbeat"


# ---------------------------------------------------------------------------- incomplete reports
# The contract says unknown values are null. A report with gaps must still become an alert.
def test_unknown_problem_type_is_accepted_and_urgent(store, report):
    incident, outcome = store.record_problem(ProblemReport(**{**report, "problemType": "unknown"}))

    assert outcome == "created"
    assert incident["incidentId"].endswith("#unknown#2")
    assert incident["severity"] == "urgent"


def test_missing_gps_is_stored_as_no_location(store, report):
    no_fix = {**report, "location": {"lat": None, "lng": None}}
    incident, _ = store.record_problem(ProblemReport(**no_fix))

    assert incident["lastLocation"] is None


def test_missing_step_is_unknown_in_the_incident_id(store, report):
    incident, outcome = store.record_problem(ProblemReport(**{**report, "stepIndex": None}))

    assert outcome == "created"
    assert incident["incidentId"] == f"{report['travellerId']}#T-001#off_route#unknown"
    assert incident["stepIndex"] is None


def test_repeated_reports_with_no_step_are_one_incident(store, report):
    no_step = ProblemReport(**{**report, "stepIndex": None})
    store.record_problem(no_step)
    incident, outcome = store.record_problem(no_step)

    assert outcome == "updated"
    assert incident["detectionCount"] == 2


# ---------------------------------------------------------------------------- states
def test_acknowledge_then_resolve(store, report):
    iid = store.record_problem(ProblemReport(**report))[0]["incidentId"]

    acked = store.acknowledge(iid, "G-007")
    assert acked["state"] == "acknowledged"
    assert acked["acknowledgedBy"] == "G-007"

    resolved = store.resolve(iid, "G-007")
    assert resolved["state"] == "resolved"
    assert resolved["expiresAt"] > datetime.now(timezone.utc).timestamp()


def test_acknowledge_twice_returns_the_same_result(store, report):
    """A retried click must not fail."""
    iid = store.record_problem(ProblemReport(**report))[0]["incidentId"]
    first = store.acknowledge(iid, "G-007")
    second = store.acknowledge(iid, "G-007")

    assert second["state"] == "acknowledged"
    assert second["acknowledgedAt"] == first["acknowledgedAt"]


def test_resolve_twice_returns_the_same_result(store, report):
    iid = store.record_problem(ProblemReport(**report))[0]["incidentId"]
    first = store.resolve(iid, "G-007")
    second = store.resolve(iid, "G-007")

    assert second["state"] == "resolved"
    assert second["resolvedAt"] == first["resolvedAt"]


def test_cannot_acknowledge_a_resolved_incident(store, report):
    iid = store.record_problem(ProblemReport(**report))[0]["incidentId"]
    store.resolve(iid, "G-007")

    with pytest.raises(ApiError) as err:
        store.acknowledge(iid, "G-007")
    assert err.value.status == 409


def test_unknown_incident_is_not_found(store):
    with pytest.raises(ApiError) as err:
        store.resolve("U-nobody#T-000#off_route#0", None)
    assert err.value.status == 404


# ---------------------------------------------------------------------------- reopening
def test_problem_detected_after_resolve_reopens_the_incident(store, report):
    iid = store.record_problem(ProblemReport(**report))[0]["incidentId"]
    store.resolve(iid, "G-007")

    later = {**report, "timestamp": (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()}
    incident, outcome = store.record_problem(ProblemReport(**later))

    assert outcome == "reopened"
    assert incident["state"] == "open"
    assert "resolvedAt" not in incident
    assert "expiresAt" not in incident


def test_late_retry_from_before_resolve_does_not_reopen(store, report):
    """A re-delivered copy of the original report (detected before the resolve) is just counted."""
    iid = store.record_problem(ProblemReport(**report))[0]["incidentId"]
    store.resolve(iid, "G-007")

    incident, outcome = store.record_problem(ProblemReport(**report))  # timestamp 2026-10-12, long before

    assert outcome == "updated"
    assert incident["state"] == "resolved"
