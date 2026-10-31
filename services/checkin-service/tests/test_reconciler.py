"""The background clean-up task and its leader lease."""
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import boto3
import fakeredis
import pytest
import redis
from moto import mock_aws

from app import aws_resources
from app.live_state import LiveState
from app.models import Checkin
from app.queue import CheckinQueue
from app.reconciler import LeaderLease, Reconciler
from app.service import CheckinService
from app.store import CheckinStore

REGION = "ap-southeast-1"
TABLE = QUEUE = "careroute-checkins"


def ts(seconds_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def checkin(seq: int, trip="T-001", **over) -> Checkin:
    body = {
        "tripId": trip, "travellerId": "U-042", "seq": seq, "timestamp": ts(),
        "lat": 1.4052, "lng": 103.9024, "accuracyM": 12, "speedMps": 1.3, "headingDeg": 85,
        "stepIndex": seq, "routeVersion": 3, "batteryPct": 64, "status": "active", "bufferedOffline": False,
    }
    body.update(over)
    return Checkin.model_validate(body)


class Env:
    def __init__(self, r, sqs, queue_url, table, service):
        self.r, self.sqs, self.queue_url, self.table, self.service = r, sqs, queue_url, table, service

    def messages(self) -> list[dict]:
        out = []
        while True:
            got = self.sqs.receive_message(QueueUrl=self.queue_url, MaxNumberOfMessages=10).get("Messages", [])
            if not got:
                return out
            for m in got:
                out.append(json.loads(m["Body"]))
                self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=m["ReceiptHandle"])

    def make_stuck(self, c: Checkin, monkeypatch) -> None:
        """First attempt stores the item, then SQS fails; the phone never retries."""
        real = self.service.queue.send
        monkeypatch.setattr(self.service.queue, "send", lambda _c: (_ for _ in ()).throw(ConnectionError("sqs down")))
        with pytest.raises(ConnectionError):
            self.service.ingest(c)
        monkeypatch.setattr(self.service.queue, "send", real)


@pytest.fixture
def env():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        sqs = boto3.client("sqs", region_name=REGION)
        aws_resources.ensure_table(ddb, TABLE)
        url = aws_resources.ensure_queues(sqs, QUEUE)
        sqs.set_queue_attributes(QueueUrl=url, Attributes={"ReceiveMessageWaitTimeSeconds": "0"})
        real = os.getenv("TEST_REDIS_URL")
        r = redis.Redis.from_url(real) if real else fakeredis.FakeRedis()
        r.flushdb()
        resource = boto3.resource("dynamodb", region_name=REGION)
        svc = CheckinService(CheckinStore(resource, TABLE, 7), LiveState(r, 86400, 10, 100), CheckinQueue(sqs, QUEUE))
        yield Env(r, sqs, url, resource.Table(TABLE), svc)


# ---------------------------------------------------------------- finishing stuck items

def test_finishes_a_checkin_whose_phone_never_retried(env, monkeypatch):
    env.make_stuck(checkin(7), monkeypatch)
    item = env.table.get_item(Key={"tripId": "T-001", "seq": 7})["Item"]
    assert item["enqueued"] is False and item["pending"] == "P"
    assert env.messages() == []

    assert env.service.reconcile_once(grace_s=0) == 1

    assert [m["seq"] for m in env.messages()] == [7]
    item = env.table.get_item(Key={"tripId": "T-001", "seq": 7})["Item"]
    assert item["enqueued"] is True and "pending" not in item
    assert env.r.get("trip:T-001:lastseq") == b"7"


def test_leaves_recent_items_to_the_phone_retry(env, monkeypatch):
    env.make_stuck(checkin(7), monkeypatch)
    assert env.service.reconcile_once(grace_s=30) == 0          # too recent: phone may still retry
    later = datetime.now(timezone.utc) + timedelta(seconds=31)
    assert env.service.reconcile_once(grace_s=30, now=later) == 1


def test_finished_checkins_are_not_in_the_index(env):
    for s in range(5):
        env.service.ingest(checkin(s))
    assert env.service.store.find_stuck("9999-12-31T00:00:00.000Z", 100) == []


def test_rereads_before_acting_because_the_index_lags(env, monkeypatch):
    """The index can still list an item another replica has just finished."""
    env.make_stuck(checkin(3), monkeypatch)
    env.table.update_item(Key={"tripId": "T-001", "seq": 3}, UpdateExpression="SET enqueued = :t",
                          ExpressionAttributeValues={":t": True})  # finished, index not yet updated
    assert env.service.reconcile_once(grace_s=0) == 0
    assert env.messages() == []


def test_phone_retry_and_clean_up_at_the_same_time_is_harmless(env, monkeypatch):
    env.make_stuck(checkin(9), monkeypatch)
    t = threading.Thread(target=lambda: env.service.reconcile_once(grace_s=0))
    t.start()
    env.service.ingest(checkin(9))  # the phone came back after all
    t.join()
    seqs = [m["seq"] for m in env.messages()]
    assert set(seqs) == {9} and 1 <= len(seqs) <= 2              # at-least-once, never lost
    item = env.table.get_item(Key={"tripId": "T-001", "seq": 9})["Item"]
    assert item["enqueued"] is True and "pending" not in item


def test_a_backlog_clears_in_one_round(env, monkeypatch):
    real = env.service.queue.send
    monkeypatch.setattr(env.service.queue, "send", lambda _c: (_ for _ in ()).throw(ConnectionError()))
    for s in range(250):
        with pytest.raises(ConnectionError):
            env.service.ingest(checkin(s, trip=f"T-{s % 7}"))
    monkeypatch.setattr(env.service.queue, "send", real)
    rec = Reconciler(env.service, LeaderLease(env.r, "a", 5000), interval_s=1, grace_s=0, batch=100)
    assert rec.tick() == 250
    assert len(env.messages()) == 250


def test_buffered_flag_survives_the_round_trip(env, monkeypatch):
    env.make_stuck(checkin(4, bufferedOffline=True), monkeypatch)
    env.service.reconcile_once(grace_s=0)
    assert env.messages()[0]["bufferedOffline"] is True


# ---------------------------------------------------------------- leader lease

def test_only_one_replica_leads(env):
    a, b = LeaderLease(env.r, "pod-a", 5000), LeaderLease(env.r, "pod-b", 5000)
    assert a.acquire_or_renew() is True
    assert b.acquire_or_renew() is False
    assert a.acquire_or_renew() is True          # renewing keeps it
    assert a.current_holder() == "pod-a"


def test_a_follower_cannot_release_the_leaders_lease(env):
    a, b = LeaderLease(env.r, "pod-a", 5000), LeaderLease(env.r, "pod-b", 5000)
    a.acquire_or_renew()
    b.release()
    assert a.current_holder() == "pod-a"


def test_clean_shutdown_hands_over_immediately(env):
    a, b = LeaderLease(env.r, "pod-a", 5000), LeaderLease(env.r, "pod-b", 5000)
    a.acquire_or_renew()
    a.release()
    assert b.acquire_or_renew() is True


def test_a_crashed_leader_is_replaced_after_the_lease_expires(env):
    a, b = LeaderLease(env.r, "pod-a", 300), LeaderLease(env.r, "pod-b", 300)
    a.acquire_or_renew()                      # then pod-a "crashes": no renew, no release
    assert b.acquire_or_renew() is False
    time.sleep(0.45)
    assert b.acquire_or_renew() is True
    assert a.acquire_or_renew() is False      # the old leader can't take it back


def test_two_replicas_cleaning_up_together_finish_everything_once_each(env, monkeypatch):
    real = env.service.queue.send
    monkeypatch.setattr(env.service.queue, "send", lambda _c: (_ for _ in ()).throw(ConnectionError()))
    for s in range(120):
        with pytest.raises(ConnectionError):
            env.service.ingest(checkin(s))
    monkeypatch.setattr(env.service.queue, "send", real)
    reps = [Reconciler(env.service, LeaderLease(env.r, f"pod-{i}", 5000), 1, 0, 50) for i in range(2)]
    results = [0, 0]
    threads = [threading.Thread(target=lambda i=i: results.__setitem__(i, reps[i].tick())) for i in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [0, 120]               # exactly one did the work
    assert sum(r.is_leader for r in reps) == 1
    assert sorted(m["seq"] for m in env.messages()) == list(range(120))


def test_redis_down_skips_the_round_without_crashing(env):
    class Broken:
        def acquire_or_renew(self):
            raise redis.ConnectionError("redis down")
    rec = Reconciler(env.service, Broken(), 1, 0, 10)  # type: ignore[arg-type]
    assert rec.tick() == 0 and rec.is_leader is False


# ---------------------------------------------------------------- table setup

def test_adds_the_index_to_a_table_from_an_older_version():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(TableName="old", BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "tripId", "AttributeType": "S"},
                                               {"AttributeName": "seq", "AttributeType": "N"}],
                         KeySchema=[{"AttributeName": "tripId", "KeyType": "HASH"},
                                    {"AttributeName": "seq", "KeyType": "RANGE"}])
        aws_resources.ensure_table(ddb, "old")
        names = [g["IndexName"] for g in ddb.describe_table(TableName="old")["Table"]["GlobalSecondaryIndexes"]]
        assert names == ["pending-index"]


def test_the_app_runs_the_clean_up_and_hands_over_on_shutdown(env, monkeypatch):
    import dataclasses
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    from app.reconciler import LEASE_KEY

    env.make_stuck(checkin(5), monkeypatch)
    s = dataclasses.replace(Settings(), reconciler_enabled=True, reconcile_interval_s=0.1, reconcile_grace_s=0)
    with TestClient(create_app(service=env.service, s=s)) as client:
        deadline = time.time() + 5
        while time.time() < deadline and not env.table.get_item(Key={"tripId": "T-001", "seq": 5})["Item"]["enqueued"]:
            time.sleep(0.05)
        assert client.app.state.reconciler.is_leader
        assert env.r.get(LEASE_KEY) is not None
    assert env.table.get_item(Key={"tripId": "T-001", "seq": 5})["Item"]["enqueued"] is True
    assert env.r.get(LEASE_KEY) is None   # released on shutdown, the next replica takes over at once
