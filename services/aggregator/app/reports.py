"""Builds problem-report.json for the Alert Service (POST /internal/problems). Pure, no I/O.

The Alert Service turns each report into one incident with the id
travellerId#tripId#problemType#stepIndex, so sending the same report again is safe.
"""
from datetime import datetime, timezone

from .voting import Decision

# Detectors said "problem" but none gave a type. The alert still goes out (a missed alert
# is the unsafe failure); the type isn't in the contract's list yet, see the contract PR.
UNKNOWN_PROBLEM = "unknown"


def iso_z(dt: datetime) -> str:
    """2026-10-12T08:15:31Z: UTC, whole seconds, as in the contract examples."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_problem_report(request_id: str, checkin: dict, votes: list[dict], decision: Decision,
                         now: datetime | None = None) -> dict:
    if decision.decision != "problem":
        raise ValueError(f"only a 'problem' decision is reported, got {decision.decision!r}")
    now = now or datetime.now(timezone.utc)
    return {
        "reportId": request_id,
        "source": "aggregator",
        "travellerId": checkin["travellerId"],
        "tripId": checkin["tripId"],
        "stepIndex": checkin.get("stepIndex"),
        "problemType": decision.problem_type or UNKNOWN_PROBLEM,
        "confidence": round(decision.confidence, 2),
        "detectorsUsed": len(votes),
        "votes": [
            {"detectorId": v.get("detectorId"), "verdict": v.get("verdict"), "confidence": v.get("confidence")}
            for v in votes
        ],
        "location": {"lat": checkin.get("lat"), "lng": checkin.get("lng")},
        "batteryPct": checkin.get("batteryPct"),
        "timestamp": iso_z(now),
    }
