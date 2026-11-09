"""Two replicas sharing one Redis, and the health endpoints."""
from dataclasses import replace
from datetime import timedelta

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.lease import LEASE_KEY, LeaderLease
from app.main import MonitorLoop, create_app
from app.monitor import HeartbeatMonitor
from tests.test_monitor import T0, Clock, FakeAlerts, stamp


@pytest.fixture
def shared():
    """One Redis with a trip that has been silent long enough to report, and two replicas."""
    r = fakeredis.FakeRedis(decode_responses=True)
    clock = Clock(T0)
    r.sadd("active-trips", "T-001")
    r.set("hb:T-001:lastseen", stamp(T0 - timedelta(seconds=120)))
    r.hset("trip:T-001:context", mapping={"travellerId": "U-042", "stepIndex": "1"})
    alerts = FakeAlerts()

    def replica(name: str) -> MonitorLoop:
        return MonitorLoop(HeartbeatMonitor(r, alerts, Settings(), clock=clock),
                           LeaderLease(r, name, ttl_ms=15_000), interval_s=5)
    return r, alerts, replica


def test_only_the_leader_checks_trips(shared):
    r, alerts, replica = shared
    a, b = replica("pod-a"), replica("pod-b")
    assert len(a.tick()) == 1 and a.is_leader
    assert b.tick() == [] and not b.is_leader
    assert r.get(LEASE_KEY) == "pod-a"
    assert len(alerts.reports) == 1


def test_another_replica_takes_over_when_the_leader_leaves(shared):
    r, alerts, replica = shared
    a, b = replica("pod-a"), replica("pod-b")
    a.tick()
    a.lease.release()
    r.delete("hb:T-001:reported")      # pretend the report hadn't been made yet
    assert len(b.tick()) == 1 and b.is_leader


def test_another_replica_takes_over_when_the_lease_expires(shared):
    r, alerts, replica = shared
    a, b = replica("pod-a"), replica("pod-b")
    a.tick()
    r.pexpire(LEASE_KEY, 1)
    r.delete(LEASE_KEY)                # fakeredis doesn't run the clock; expiry = key gone
    b.tick()
    assert b.is_leader and r.get(LEASE_KEY) == "pod-b"


def test_a_failing_round_does_not_stop_the_loop(shared):
    r, alerts, replica = shared
    a = replica("pod-a")
    a.monitor.tick = lambda: 1 / 0
    assert a.tick() == []
    assert a.is_leader


# ---------------------------------------------------------------------- endpoints
def make_client(r) -> TestClient:
    return TestClient(create_app(client=r, alerts=FakeAlerts(), s=replace(Settings(), monitor_enabled=False)))


def test_health():
    with make_client(fakeredis.FakeRedis()) as c:
        assert c.get("/health").json() == {"status": "ok"}


def test_ready_when_redis_is_reachable():
    with make_client(fakeredis.FakeRedis()) as c:
        assert c.get("/ready").status_code == 200


def test_not_ready_when_redis_is_down():
    server = fakeredis.FakeServer()
    server.connected = False
    with make_client(fakeredis.FakeRedis(server=server)) as c:
        resp = c.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "not_ready"


def test_starts_and_stops_the_loop_with_the_app():
    r = fakeredis.FakeRedis(decode_responses=True)
    app = create_app(client=r, alerts=FakeAlerts(), s=replace(Settings(), tick_s=0.05))
    with TestClient(app):
        assert app.state.loop is not None
    assert r.get(LEASE_KEY) is None    # released on shutdown, so another replica takes over at once
