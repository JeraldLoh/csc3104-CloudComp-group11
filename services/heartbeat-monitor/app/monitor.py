"""Checks every active trip for a traveller who has gone silent, and tells the Alert Service.

Each tick (every TICK_S seconds, on the leader replica only) and for every trip in `active-trips`:

1. Read what the Check-in Service recorded (contracts/redis-keys.md):
     hb:{tripId}:lastseen    when the last live check-in arrived (server time, so phone clocks don't matter)
     hb:{tripId}:intervals   the last 100 gaps between live check-ins, in ms
     hb:{tripId}:status      "active" or "backgrounded"
     trip:{tripId}:context   travellerId and current stepIndex
     trip:{tripId}:route     the steps, to see whether the current one is underground
     trip:{tripId}:recent    newest check-in first, for the last known location and battery
2. Work out φ for the silence so far (phi.py), with a longer acceptable pause on an underground step or
   while the app is in the background.
3. If φ ≥ PHI_THRESHOLD, send problem-report-no-response.json to the Alert Service, once per silence.

"Once per silence": after a report is accepted, hb:{tripId}:reported is set to the lastseen value it was
about. Until a new check-in changes lastseen, the same silence isn't reported again. If the Alert Service
doesn't accept the report (down, timeout), nothing is marked, so the next tick tries again.
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import redis

from .alerts import AlertClient
from .config import Settings
from .phi import phi, rhythm

log = logging.getLogger("heartbeat.monitor")

ACTIVE_TRIPS = "active-trips"


def iso(dt: datetime) -> str:
    """contracts/README.md: ISO 8601 in UTC, ending in Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _text(v) -> str | None:
    return v.decode() if isinstance(v, bytes) else v


def _int_or_none(v) -> int | None:
    """The Check-in Service stores a null stepIndex as "" (Redis hashes can't hold null)."""
    v = _text(v)
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


@dataclass(frozen=True)
class Snapshot:
    """What Redis holds about one trip right now."""
    trip_id: str
    last_seen_raw: str | None
    intervals_ms: list[float]
    status: str | None
    traveller_id: str | None
    step_index: int | None
    route_json: str | None
    latest_checkin: dict | None
    reported: str | None

    @property
    def last_seen(self) -> datetime | None:
        return _parse_iso(self.last_seen_raw) if self.last_seen_raw else None


@dataclass(frozen=True)
class Assessment:
    trip_id: str
    phi: float
    elapsed_s: float
    pause_s: float
    underground: bool
    backgrounded: bool
    suspected: bool


def step_is_underground(route_json: str | None, step_index: int | None) -> bool:
    """True if the traveller's current step is (or may be) underground.

    The Journey Service sets `underground: null` on MRT steps because OneMap doesn't say which stretches
    are in a tunnel. A train step we can't be sure about counts as underground: a later alert on an
    above-ground train is better than a false "no response" on every ride through a tunnel.
    """
    if step_index is None or not route_json:
        return False
    try:
        steps = json.loads(route_json).get("steps") or []
    except (ValueError, AttributeError):
        log.warning("route JSON for step %s couldn't be read; treating the step as above ground", step_index)
        return False
    step = next((s for s in steps if s.get("stepIndex") == step_index), None)
    if step is None:
        return False
    flag = step.get("underground")
    if flag is None:
        return step.get("mode") == "train"
    return bool(flag)


class HeartbeatMonitor:
    def __init__(self, client: redis.Redis, alerts: AlertClient, s: Settings,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.r = client
        self.alerts = alerts
        self.s = s
        self.clock = clock

    # ------------------------------------------------------------------ reading
    def snapshot(self, trip_id: str) -> Snapshot:
        pipe = self.r.pipeline(transaction=False)
        pipe.get(f"hb:{trip_id}:lastseen")
        pipe.lrange(f"hb:{trip_id}:intervals", 0, -1)
        pipe.get(f"hb:{trip_id}:status")
        pipe.hgetall(f"trip:{trip_id}:context")
        pipe.get(f"trip:{trip_id}:route")
        pipe.lindex(f"trip:{trip_id}:recent", 0)
        pipe.get(f"hb:{trip_id}:reported")
        last_seen, intervals, status, context, route, latest, reported = pipe.execute()

        gaps = []
        for g in intervals or []:
            try:
                gaps.append(float(_text(g)))
            except (TypeError, ValueError):
                continue
        context = {_text(k): _text(v) for k, v in (context or {}).items()}
        try:
            latest_checkin = json.loads(_text(latest)) if latest else None
        except ValueError:
            latest_checkin = None
        return Snapshot(
            trip_id=trip_id,
            last_seen_raw=_text(last_seen),
            intervals_ms=gaps,
            status=_text(status),
            traveller_id=context.get("travellerId") or None,
            step_index=_int_or_none(context.get("stepIndex")),
            route_json=_text(route),
            latest_checkin=latest_checkin,
            reported=_text(reported),
        )

    # ------------------------------------------------------------------ deciding
    def assess(self, snap: Snapshot, now: datetime) -> Assessment:
        s = self.s
        underground = step_is_underground(snap.route_json, snap.step_index)
        backgrounded = snap.status == "backgrounded"
        extra = max(s.underground_pause_s if underground else 0.0, s.backgrounded_pause_s if backgrounded else 0.0)
        pause_s = s.base_pause_s + extra
        elapsed_s = max(0.0, (now - snap.last_seen).total_seconds())
        r = rhythm(snap.intervals_ms, expected_interval_ms=s.expected_interval_ms,
                   min_samples=s.min_samples, min_std_ms=s.min_std_ms)
        value = phi(elapsed_s * 1000, r, pause_s * 1000)
        return Assessment(snap.trip_id, value, elapsed_s, pause_s, underground, backgrounded,
                          suspected=value >= s.phi_threshold)

    # ------------------------------------------------------------------ reporting
    def build_report(self, snap: Snapshot, a: Assessment, now: datetime) -> dict:
        """contracts/examples/problem-report-no-response.json. Unknown values are null."""
        latest = snap.latest_checkin or {}
        detected = iso(now)
        return {
            "reportId": f"{snap.trip_id}#hb#{detected}",
            "source": "heartbeat",
            "travellerId": snap.traveller_id,
            "tripId": snap.trip_id,
            "stepIndex": snap.step_index,
            "problemType": "no_response",
            "confidence": None,
            "phi": round(a.phi, 1),
            "lastSeenAt": iso(snap.last_seen),
            "location": {"lat": latest.get("lat"), "lng": latest.get("lng")},
            "batteryPct": latest.get("batteryPct"),
            "timestamp": detected,
        }

    def check_trip(self, trip_id: str, now: datetime) -> dict | None:
        """Report this trip if it has newly gone silent. Returns the report sent, if any."""
        snap = self.snapshot(trip_id)
        if snap.last_seen is None:
            return None  # no check-in yet: nothing to measure the silence from
        a = self.assess(snap, now)
        if not a.suspected:
            return None
        if snap.reported == snap.last_seen_raw:
            return None  # this silence has already been reported
        if not snap.traveller_id:
            log.warning("trip %s is silent (phi %.1f) but has no travellerId in its context; can't report it",
                        trip_id, a.phi)
            return None
        report = self.build_report(snap, a, now)
        result = self.alerts.report(report)
        self.r.set(f"hb:{trip_id}:reported", snap.last_seen_raw, ex=self.s.trip_key_ttl_s)
        log.warning("no response from trip %s: silent %.0f s (allowed pause %.0f s%s%s), phi %.1f -> incident %s",
                    trip_id, a.elapsed_s, a.pause_s, ", underground" if a.underground else "",
                    ", backgrounded" if a.backgrounded else "", a.phi, result.get("incidentId"))
        return report

    def tick(self) -> list[dict]:
        """Check every active trip once. One trip failing doesn't stop the others."""
        now = self.clock()
        sent = []
        for trip_id in sorted(_text(t) for t in self.r.smembers(ACTIVE_TRIPS)):
            try:
                report = self.check_trip(trip_id, now)
            except Exception as e:  # noqa: BLE001 - Alert Service down, bad data: try again next tick
                log.warning("couldn't check trip %s: %s", trip_id, e)
                continue
            if report:
                sent.append(report)
        return sent
