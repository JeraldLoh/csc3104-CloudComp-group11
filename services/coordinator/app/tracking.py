"""Tracking lost: for some problem types couldn't be checked at all.

So the caregiver knows to ask the traveller with "Where are you?" (Alert Service, Member 5).

A check-in leaves types unchecked (unchecked()) when:
  - route missing: the decision is inconclusive and the trip's route isn't in Redis, so no
    detector can judge anything; or
  - poor GPS (untrackable()): the decision is inconclusive, GPS accuracy is missing or worse
    than GPS_MAX_ACCURACY_M (the detectors' own limit), and the step is not underground: no GPS
    is expected there, and the Heartbeat Monitor already allows for it. Same rule as the
    Heartbeat Monitor: `underground: true`, or a train step with `underground: null` (OneMap
    doesn't say which stretches are in a tunnel); or
  - detectors failed: every detector that checks a type (voting.COVERAGE) failed (timeout,
    error; risk.failed), e.g. dwell-time down leaves long_dwell unchecked while the others
    still decide. Kubernetes restarts a crashed pod; this catches a detector that is up but
    not answering properly. A silenced detector (reliability 0) counts as failed too: it
    answers, but nothing it says is used, so its types are just as unchecked.
The streak starts at the first such check-in (untracked:{tripId}) and any fully checked one ends it.
"""
import json
from datetime import datetime

from .risk import failed
from .voting import COVERAGE, Decision

TRACKING_LOST = "tracking_lost"
ALL_TYPES = sorted(set().union(*COVERAGE.values()))

ROUTE_MISSING, POOR_GPS = "route missing", "poor GPS"
DETECTORS_FAILED, DETECTORS_SILENCED = "detectors failed", "detectors silenced"


def untracked_key(trip_id: str) -> str:
    return f"untracked:{trip_id}"


def poor_gps(checkin: dict, max_accuracy_m: float) -> bool:
    try:
        return float(checkin["accuracyM"]) > max_accuracy_m
    except (KeyError, TypeError, ValueError):
        return True   # no accuracy: no usable fix


def step_underground(route_json: str | None, step_index) -> bool:
    if not route_json or step_index is None:
        return False
    try:
        steps = json.loads(route_json).get("steps") or []
        i = int(step_index)
    except (ValueError, TypeError, AttributeError):
        return False
    step = next((s for s in steps if isinstance(s, dict) and s.get("stepIndex") == i), None)
    if step is None and 0 <= i < len(steps) and isinstance(steps[i], dict):
        step = steps[i]
    if step is None:
        return False
    flag = step.get("underground")
    if flag is None:
        return str(step.get("mode") or "").lower() in ("train", "mrt")
    return bool(flag)


def untrackable(d: Decision, checkin: dict, route_json: str | None, max_accuracy_m: float) -> bool:
    return (d.decision == "inconclusive" and poor_gps(checkin, max_accuracy_m)
            and not step_underground(route_json, checkin.get("stepIndex")))


def failed_types(votes: list[dict], silenced: tuple = (), coverage: dict[str, frozenset] = COVERAGE) -> list[str]:
    """Types whose every asked detector that checks them failed or is silenced. A type none of
    them was asked about isn't counted: in adaptive mode that's a choice, not a failure."""
    asked = {v.get("detectorId") for v in votes}
    down = {v.get("detectorId") for v in votes if failed(v)} | (set(silenced) & asked)
    unchecked = []
    for problem_type in ALL_TYPES:
        judges = {d for d, types in coverage.items() if problem_type in types} & asked
        if judges and judges <= down:
            unchecked.append(problem_type)
    return unchecked


def unchecked(d: Decision, checkin: dict, votes: list[dict], route_json: str | None,
              max_accuracy_m: float) -> tuple[str, list[str]] | None:
    """Why this check-in left problem types unchecked, and which; None if every type was checked."""
    if d.decision == "inconclusive" and route_json is None:
        return ROUTE_MISSING, ALL_TYPES
    if untrackable(d, checkin, route_json, max_accuracy_m):
        return POOR_GPS, ALL_TYPES
    types = failed_types(votes, d.silenced)
    if not types:
        return None
    # "failed" if any of it is a real failure; only silencing is an operator's choice.
    return (DETECTORS_FAILED if failed_types(votes) else DETECTORS_SILENCED), types


def seconds_since(since: str, timestamp: str) -> float:
    """Seconds between two check-in timestamps (ISO 8601, ...Z); 0 if either can't be read."""
    try:
        a = datetime.fromisoformat(since.replace("Z", "+00:00"))
        b = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return 0.0
    return (b - a).total_seconds()
