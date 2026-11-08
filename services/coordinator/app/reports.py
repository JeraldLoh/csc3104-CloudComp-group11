"""Builds problem-report.json for the Alert Service (POST /internal/problems). Pure, no I/O.

The Alert Service turns each report into one incident with the id
travellerId#tripId#problemType#stepIndex, so sending the same report again is safe.
"""
from datetime import datetime, timezone

from .tracking import TRACKING_LOST
from .voting import Decision

# Detectors said "problem" but none gave a type. The alert still goes out (a missed alert
# is the unsafe failure); the type isn't in the contract's list yet, see the contract PR.
UNKNOWN_PROBLEM = "unknown"


def whole_number(value) -> int | None:
    """batteryPct as an int: the Alert Service only accepts whole numbers, but a check-in may
    carry 63.5. Rejecting the report over that would lose the alert. Unreadable -> null."""
    try:
        return None if value is None else int(round(float(value)))
    except (TypeError, ValueError):
        return None


def iso_z(dt: datetime) -> str:
    """2026-10-12T08:15:31Z: UTC, whole seconds, as in the contract examples."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_problem_reports(request_id: str, checkin: dict, votes: list[dict], decision: Decision,
                          now: datetime | None = None) -> list[dict]:
    """One report per problem type raised, strongest first. Two detectors can find different
    problems on the same check-in (walking sideways off the road can also look like the wrong
    direction); reporting only the strongest would drop the other, maybe for the whole trip."""
    if decision.decision != "problem":
        raise ValueError(f"only a 'problem' decision is reported, got {decision.decision!r}")
    now = now or datetime.now(timezone.utc)
    claims = decision.claims or ((decision.problem_type, decision.confidence),)
    return [_report(request_id, checkin, votes, problem_type, weight, now) for problem_type, weight in claims]


def build_problem_report(request_id: str, checkin: dict, votes: list[dict], decision: Decision,
                         now: datetime | None = None) -> dict:
    """The report for the strongest problem raised."""
    return build_problem_reports(request_id, checkin, votes, decision, now)[0]


def build_tracking_lost_report(request_id: str, checkin: dict, votes: list[dict], reason: str,
                               unchecked: list[str], now: datetime | None = None) -> dict:
    """tracking.py: some problem types couldn't be checked for too long. Raised by the Coordinator,
    not a detector, so the confidence is 1.0: we're sure we can't check. No stepIndex, so the
    Alert Service keeps one incident per trip however many steps it lasts. `reason` and
    `unchecked` are extra fields (the Alert Service ignores them) for logs and the experiment."""
    report = _report(request_id, checkin, votes, TRACKING_LOST, 1.0, now or datetime.now(timezone.utc))
    return {**report, "stepIndex": None, "reason": reason, "unchecked": unchecked}


def _report(request_id: str, checkin: dict, votes: list[dict], problem_type: str | None, confidence: float,
            now: datetime) -> dict:
    return {
        "reportId": request_id,
        "source": "coordinator",
        "travellerId": checkin["travellerId"],
        "tripId": checkin["tripId"],
        "stepIndex": checkin.get("stepIndex"),
        "problemType": problem_type or UNKNOWN_PROBLEM,
        "confidence": round(confidence, 2),
        "detectorsUsed": len(votes),
        "votes": [
            {"detectorId": v.get("detectorId"), "verdict": v.get("verdict"), "confidence": v.get("confidence")}
            for v in votes
        ],
        "location": {"lat": checkin.get("lat"), "lng": checkin.get("lng")},
        "batteryPct": whole_number(checkin.get("batteryPct")),
        "timestamp": iso_z(now),
    }
