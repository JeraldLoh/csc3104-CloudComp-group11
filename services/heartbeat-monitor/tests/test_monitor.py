"""The monitor against Redis data shaped exactly as the Check-in and Journey Services write it."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fakeredis
import pytest

from app.config import Settings
from app.monitor import HeartbeatMonitor, step_is_underground

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "examples" / "problem-report-no-response.json"
T0 = datetime(2026, 10, 12, 8, 15, 0, tzinfo=timezone.utc)

ROUTE = {"steps": [
    {"stepIndex": 0, "mode": "walk", "underground": False},
    {"stepIndex": 1, "mode": "bus", "underground": False},
    {"stepIndex": 2, "mode": "train", "underground": True},
    {"stepIndex": 3, "mode": "train", "underground": None},   # what the Journey Service sets for MRT
]}


class FakeAlerts:
    def __init__(self):
        self.reports: list[dict] = []
        self.fail = False

    def report(self, report: dict) -> dict:
        if self.fail:
            raise ConnectionError("alert-service unreachable")
        self.reports.append(report)
        return {"incidentId": f"{report['travellerId']}#{report['tripId']}#no_response#{report['stepIndex']}",
                "created": True}

    def close(self):
        pass


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float):
        self.now += timedelta(seconds=seconds)


def stamp(dt: datetime) -> str:
    """hb:{tripId}:lastseen as the Check-in Service writes it (live_state.py)."""
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@pytest.fixture
def env():
    r = fakeredis.FakeRedis(decode_responses=True)
    alerts = FakeAlerts()
    clock = Clock(T0)
    monitor = HeartbeatMonitor(r, alerts, Settings(), clock=clock)

    def trip(trip_id="T-001", *, step=1, status="active", seen_ago=0.0, gaps=(10_000,) * 20, active=True,
             traveller="U-042", route=ROUTE, latest=None):
        if active:
            r.sadd("active-trips", trip_id)
        r.set(f"hb:{trip_id}:lastseen", stamp(clock.now - timedelta(seconds=seen_ago)))
        if gaps:
            r.rpush(f"hb:{trip_id}:intervals", *[str(g) for g in gaps])
        r.set(f"hb:{trip_id}:status", status)
        r.hset(f"trip:{trip_id}:context", mapping={
            "travellerId": traveller or "", "stepIndex": "" if step is None else str(step),
            "routeVersion": "3", "status": status})
        if route is not None:
            r.set(f"trip:{trip_id}:route", json.dumps(route))
        r.lpush(f"trip:{trip_id}:recent", json.dumps(latest or {
            "tripId": trip_id, "seq": 17, "lat": 1.4052, "lng": 103.9024, "batteryPct": 63}))

    def check_in(trip_id="T-001"):
        """A new live check-in arrives now."""
        r.set(f"hb:{trip_id}:lastseen", stamp(clock.now))

    class Env:
        pass
    e = Env()
    e.r, e.alerts, e.clock, e.monitor, e.trip, e.check_in = r, alerts, clock, monitor, trip, check_in
    return e


def silent_for(env, seconds: float) -> list[dict]:
    env.clock.advance(seconds)
    return env.monitor.tick()


# ---------------------------------------------------------------------- deciding
def test_a_traveller_checking_in_normally_is_left_alone(env):
    env.trip()
    for _ in range(30):
        assert silent_for(env, 10) == []
        env.check_in()


def test_reports_a_silence_well_past_the_usual_rhythm(env):
    env.trip()
    assert silent_for(env, 45) == []
    sent = silent_for(env, 10)          # 55 s: past the ~50 s point for a 10 s rhythm
    assert len(sent) == 1
    assert sent[0]["phi"] >= 8


def test_report_matches_the_contract_example(env):
    env.trip(step=1)
    report = silent_for(env, 60)[0]
    example = json.loads(CONTRACT.read_text())
    assert set(report) == set(example)
    assert report == {
        "reportId": "T-001#hb#2026-10-12T08:16:00Z",
        "source": "heartbeat",
        "travellerId": "U-042",
        "tripId": "T-001",
        "stepIndex": 1,
        "problemType": "no_response",
        "confidence": None,
        "phi": report["phi"],
        "lastSeenAt": "2026-10-12T08:15:00Z",
        "location": {"lat": 1.4052, "lng": 103.9024},
        "batteryPct": 63,
        "timestamp": "2026-10-12T08:16:00Z",
    }


def test_a_patchy_connection_gets_more_slack_than_a_steady_one(env):
    env.trip("T-STEADY", gaps=(10_000,) * 20)
    env.trip("T-PATCHY", gaps=(5_000, 25_000) * 10)   # mean 15 s, but spread 10 s instead of the 2 s floor
    sent = {rep["tripId"] for rep in silent_for(env, 75)}
    assert sent == {"T-STEADY"}


def test_a_new_trip_uses_the_app_rhythm_until_it_has_history(env):
    env.trip(gaps=(10_000,))            # one gap: not enough to learn from
    assert silent_for(env, 40) == []
    assert len(silent_for(env, 30)) == 1


# ---------------------------------------------------------------------- underground and background
def test_allows_a_longer_gap_on_an_underground_step(env):
    env.trip(step=2)                    # underground: true
    assert silent_for(env, 120) == []   # would already have alerted above ground
    assert silent_for(env, 200) == []   # 5 min 20 s
    assert len(silent_for(env, 40)) == 1  # 6 min


def test_a_train_step_with_unknown_underground_counts_as_underground(env):
    env.trip(step=3)                    # underground: null, mode: train
    assert silent_for(env, 120) == []


def test_a_bus_step_with_unknown_underground_does_not(env):
    route = {"steps": [{"stepIndex": 1, "mode": "bus", "underground": None}]}
    env.trip(step=1, route=route)
    assert len(silent_for(env, 60)) == 1


def test_allows_a_much_longer_gap_while_the_app_is_in_the_background(env):
    env.trip(status="backgrounded")
    assert silent_for(env, 600) == []
    assert len(silent_for(env, 420)) == 1   # 17 min: past the 15 min allowance


def test_unknown_step_or_missing_route_is_treated_as_above_ground(env):
    env.trip("T-NOSTEP", step=None)
    env.trip("T-NOROUTE", step=2, route=None)
    assert {rep["tripId"] for rep in silent_for(env, 60)} == {"T-NOSTEP", "T-NOROUTE"}


@pytest.mark.parametrize("route_json, step, expected", [
    (json.dumps(ROUTE), 0, False),
    (json.dumps(ROUTE), 2, True),
    (json.dumps(ROUTE), 3, True),
    (json.dumps(ROUTE), 9, False),       # step not in the route
    ("not json", 2, False),
    (None, 2, False),
])
def test_step_is_underground(route_json, step, expected):
    assert step_is_underground(route_json, step) is expected


# ---------------------------------------------------------------------- once per silence
def test_reports_each_silence_once(env):
    env.trip()
    assert len(silent_for(env, 60)) == 1
    assert silent_for(env, 60) == []    # still silent: already reported
    assert silent_for(env, 600) == []


def test_a_new_silence_after_the_traveller_comes_back_is_reported_again(env):
    env.trip()
    assert len(silent_for(env, 60)) == 1
    env.check_in()
    assert silent_for(env, 10) == []
    env.check_in()
    assert len(silent_for(env, 60)) == 1


def test_retries_when_the_alert_service_does_not_accept_the_report(env):
    env.trip()
    env.alerts.fail = True
    assert silent_for(env, 60) == []
    assert env.r.get("hb:T-001:reported") is None
    env.alerts.fail = False
    assert len(silent_for(env, 5)) == 1
    assert env.r.get("hb:T-001:reported") == stamp(T0)


def test_marks_the_silence_reported_with_a_ttl(env):
    env.trip()
    silent_for(env, 60)
    assert 0 < env.r.ttl("hb:T-001:reported") <= 86400


# ---------------------------------------------------------------------- which trips
def test_ignores_trips_that_are_not_active(env):
    env.trip(active=False)
    assert silent_for(env, 600) == []


def test_ignores_an_active_trip_with_no_check_in_yet(env):
    env.r.sadd("active-trips", "T-NEW")
    assert silent_for(env, 600) == []


def test_skips_a_trip_whose_context_has_no_traveller(env):
    env.trip("T-ANON", traveller=None)
    env.trip("T-001")
    assert [rep["tripId"] for rep in silent_for(env, 60)] == ["T-001"]


def test_one_broken_trip_does_not_stop_the_others(env):
    env.trip("T-001")
    env.trip("T-BAD")
    env.r.set("hb:T-BAD:lastseen", "not a time")
    assert [rep["tripId"] for rep in silent_for(env, 60)] == ["T-001"]


def test_unknown_location_and_battery_are_null(env):
    env.trip(latest={"tripId": "T-001", "seq": 3, "lat": None, "lng": None, "batteryPct": None})
    report = silent_for(env, 60)[0]
    assert report["location"] == {"lat": None, "lng": None}
    assert report["batteryPct"] is None


def test_thresholds_come_from_settings(env):
    env.monitor.s = replace(Settings(), phi_threshold=2.0, base_pause_s=0)
    env.trip()
    assert len(silent_for(env, 15)) == 1
