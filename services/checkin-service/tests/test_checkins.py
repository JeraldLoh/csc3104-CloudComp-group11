"""Tests run without Docker: moto fakes DynamoDB and SQS, fakeredis fakes Redis (with Lua)."""
import dataclasses
import json
import os
from datetime import datetime, timedelta, timezone

import boto3
import fakeredis
import pytest
import redis
from fastapi.testclient import TestClient
from moto import mock_aws

from app import aws_resources
from app.config import Settings
from app.live_state import LiveState
from app.main import create_app
from app.queue import CheckinQueue
from app.service import CheckinService
from app.store import CheckinStore

REGION = "ap-southeast-1"
TABLE = "careroute-checkins"
QUEUE = "careroute-checkins"


def ts(seconds_ago: float = 0) -> str:
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def checkin(seq: int, **over) -> dict:
    """Same shape as contracts/examples/checkin.json."""
    body = {
        "tripId": "T-001", "travellerId": "U-042", "seq": seq, "timestamp": ts(),
        "lat": 1.4052, "lng": 103.9024, "accuracyM": 12, "speedMps": 1.3, "headingDeg": 85,
        "stepIndex": 2, "routeVersion": 3, "batteryPct": 64, "status": "active", "bufferedOffline": False,
    }
    body.update(over)
    return body


class Env:
    def __init__(self, client, redis_client, sqs, queue_url, table, service):
        self.client, self.r, self.sqs, self.queue_url, self.table, self.service = (
            client, redis_client, sqs, queue_url, table, service)

    def messages(self) -> list[dict]:
        out = []
        while True:
            got = self.sqs.receive_message(QueueUrl=self.queue_url, MaxNumberOfMessages=10).get("Messages", [])
            if not got:
                return out
            for m in got:
                out.append(json.loads(m["Body"]))
                self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=m["ReceiptHandle"])


@pytest.fixture
def env():
    with mock_aws():
        ddb_client = boto3.client("dynamodb", region_name=REGION)
        sqs = boto3.client("sqs", region_name=REGION)
        aws_resources.ensure_table(ddb_client, TABLE)
        queue_url = aws_resources.ensure_queues(sqs, QUEUE)
        sqs.set_queue_attributes(QueueUrl=queue_url, Attributes={"ReceiveMessageWaitTimeSeconds": "0"})

        # Set TEST_REDIS_URL to run against a real Redis (checks the Lua scripts for real).
        real = os.getenv("TEST_REDIS_URL")
        r = redis.Redis.from_url(real) if real else fakeredis.FakeRedis()
        r.flushdb()
        service = CheckinService(
            store=CheckinStore(boto3.resource("dynamodb", region_name=REGION), TABLE, 7),
            state=LiveState(r, 86400, 10, 100),
            queue=CheckinQueue(sqs, QUEUE),
        )
        # Clean-up task off here; it has its own tests (test_reconciler.py).
        app = create_app(service=service, s=dataclasses.replace(Settings(), reconciler_enabled=False))
        with TestClient(app, raise_server_exceptions=False) as client:
            yield Env(client, r, sqs, queue_url,
                      boto3.resource("dynamodb", region_name=REGION).Table(TABLE), service)


# ---------- the happy path ----------

def test_checkin_is_stored_queued_and_visible_in_redis(env):
    res = env.client.post("/api/checkins", json=checkin(17))
    assert res.status_code == 202
    assert res.json() == {"accepted": 1, "duplicates": 0}

    item = env.table.get_item(Key={"tripId": "T-001", "seq": 17})["Item"]
    assert item["enqueued"] is True and "expiresAt" in item

    msgs = env.messages()
    assert len(msgs) == 1 and msgs[0]["seq"] == 17 and msgs[0]["tripId"] == "T-001"
    assert msgs[0]["timestamp"].endswith("Z")

    ctx = {k.decode(): v.decode() for k, v in env.r.hgetall("trip:T-001:context").items()}
    assert ctx == {"travellerId": "U-042", "stepIndex": "2", "routeVersion": "3", "status": "active"}
    assert env.r.get("trip:T-001:lastseq") == b"17"
    assert env.r.get("hb:T-001:status") == b"active"
    assert env.r.get("hb:T-001:lastseen") is not None
    assert env.r.ttl("trip:T-001:context") > 0


def test_health_and_ready(env):
    assert env.client.get("/health").json() == {"status": "ok"}
    assert env.client.get("/ready").json() == {"status": "ready"}


# ---------- duplicates and retries ----------

def test_retry_of_same_seq_is_ignored(env):
    env.client.post("/api/checkins", json=checkin(17))
    res = env.client.post("/api/checkins", json=checkin(17))
    assert res.status_code == 202
    assert res.json() == {"accepted": 0, "duplicates": 1}
    assert len(env.messages()) == 1
    assert env.r.llen("trip:T-001:recent") == 1


def test_failure_after_storing_is_finished_by_the_retry(env, monkeypatch):
    """SQS is down on the first attempt. The app gets 503, retries, and the check-in
    still reaches the queue exactly once."""
    real_send = env.service.queue.send
    monkeypatch.setattr(env.service.queue, "send", lambda c: (_ for _ in ()).throw(ConnectionError("sqs down")))
    first = env.client.post("/api/checkins", json=checkin(17))
    assert first.status_code == 503
    assert first.json()["error"]["code"] == "unavailable"
    assert env.table.get_item(Key={"tripId": "T-001", "seq": 17})["Item"]["enqueued"] is False
    assert env.messages() == []

    monkeypatch.setattr(env.service.queue, "send", real_send)
    retry = env.client.post("/api/checkins", json=checkin(17))
    assert retry.status_code == 202 and retry.json() == {"accepted": 1, "duplicates": 0}
    assert [m["seq"] for m in env.messages()] == [17]
    assert env.table.get_item(Key={"tripId": "T-001", "seq": 17})["Item"]["enqueued"] is True
    # The heartbeat was counted once, on the first attempt.
    assert env.r.llen("hb:T-001:intervals") == 0


def test_redis_down_returns_503_and_retry_recovers(env, monkeypatch):
    real_apply = env.service.state.apply
    monkeypatch.setattr(env.service.state, "apply", lambda c: (_ for _ in ()).throw(ConnectionError("redis down")))
    assert env.client.post("/api/checkins", json=checkin(5)).status_code == 503
    monkeypatch.setattr(env.service.state, "apply", real_apply)
    assert env.client.post("/api/checkins", json=checkin(5)).json() == {"accepted": 1, "duplicates": 0}
    assert env.r.get("trip:T-001:lastseq") == b"5"


# ---------- ordering ----------

def test_older_checkin_never_overwrites_newer_context(env):
    env.client.post("/api/checkins", json=checkin(20, stepIndex=4))
    env.client.post("/api/checkins", json=checkin(19, stepIndex=3))
    assert env.r.hget("trip:T-001:context", "stepIndex") == b"4"
    assert env.r.get("trip:T-001:lastseq") == b"20"


def test_recent_list_is_newest_first_and_capped_at_10(env):
    for seq in [3, 1, 12, 7, 2, 11, 5, 4, 9, 6, 8, 10, 0]:
        env.client.post("/api/checkins", json=checkin(seq))
    seqs = [json.loads(x)["seq"] for x in env.r.lrange("trip:T-001:recent", 0, -1)]
    assert seqs == [12, 11, 10, 9, 8, 7, 6, 5, 4, 3]


# ---------- offline batches and heartbeat ----------

def test_batch_after_offline(env):
    env.client.post("/api/checkins", json=checkin(17))
    env.messages()
    batch = {
        "tripId": "T-001", "travellerId": "U-042",
        "checkins": [  # sent out of order on purpose; service applies oldest first
            {k: v for k, v in checkin(19, timestamp=ts(30), bufferedOffline=True).items() if k not in ("tripId", "travellerId")},
            {k: v for k, v in checkin(18, timestamp=ts(40), bufferedOffline=True).items() if k not in ("tripId", "travellerId")},
            {k: v for k, v in checkin(17).items() if k not in ("tripId", "travellerId")},  # already have this one
        ],
    }
    res = env.client.post("/api/checkins/batch", json=batch)
    assert res.status_code == 202
    assert res.json() == {"accepted": 2, "duplicates": 1}

    msgs = env.messages()
    assert sorted(m["seq"] for m in msgs) == [18, 19]
    assert all(m["bufferedOffline"] for m in msgs)
    assert env.r.get("trip:T-001:lastseq") == b"19"


def test_buffered_checkins_do_not_add_heartbeat_intervals(env):
    env.client.post("/api/checkins", json=checkin(1))
    env.client.post("/api/checkins", json=checkin(2))          # live: one interval
    assert env.r.llen("hb:T-001:intervals") == 1
    batch = {"tripId": "T-001", "travellerId": "U-042", "checkins": [
        {k: v for k, v in checkin(s, timestamp=ts(60 - s), bufferedOffline=True).items()
         if k not in ("tripId", "travellerId")} for s in range(3, 9)]}
    before = env.r.get("hb:T-001:lastseen")
    env.client.post("/api/checkins/batch", json=batch)
    assert env.r.llen("hb:T-001:intervals") == 1                 # unchanged
    assert env.r.get("hb:T-001:lastseen") >= before              # but phone marked as seen


def test_backgrounded_status_reaches_heartbeat_monitor(env):
    env.client.post("/api/checkins", json=checkin(1, status="backgrounded"))
    assert env.r.get("hb:T-001:status") == b"backgrounded"


# ---------- validation ----------

@pytest.mark.parametrize("bad", [
    {"seq": -1}, {"lat": 95}, {"tripId": "001"}, {"travellerId": "G-003"},
    {"status": "sleeping"}, {"timestamp": "2026-10-12T08:15:30"},  # no timezone
])
def test_invalid_checkins_rejected_in_contract_error_format(env, bad):
    res = env.client.post("/api/checkins", json={**checkin(1), **bad})
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "invalid_request"


def test_future_timestamp_rejected(env):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert env.client.post("/api/checkins", json=checkin(1, timestamp=future)).status_code == 422


def test_unknown_extra_fields_are_ignored(env):
    """Contracts say adding optional fields later is safe."""
    assert env.client.post("/api/checkins", json=checkin(1, newField="x")).status_code == 202


def test_nulls_are_accepted(env):
    res = env.client.post("/api/checkins", json=checkin(1, speedMps=None, headingDeg=None, stepIndex=None))
    assert res.status_code == 202
    assert env.r.hget("trip:T-001:context", "stepIndex") == b""


# ---------- concurrency ----------

def test_concurrent_retries_and_out_of_order_arrivals(env):
    """60 check-ins, each sent twice, from 16 threads at once, in shuffled order.
    Every check-in must reach the queue, context must end on the newest one,
    and the recent list must still be in order."""
    import random
    from concurrent.futures import ThreadPoolExecutor

    sends = [s for s in range(60) for _ in range(2)]
    random.Random(7).shuffle(sends)
    with ThreadPoolExecutor(max_workers=16) as pool:
        responses = list(pool.map(lambda s: env.client.post("/api/checkins", json=checkin(s, stepIndex=s)), sends))

    bad = [(r.status_code, r.text[:200]) for r in responses if r.status_code != 202]
    assert not bad, f"non-202 answers: {bad[:5]}"
    msgs = env.messages()
    missing = set(range(60)) - set(m["seq"] for m in msgs)
    assert not missing, f"lost check-ins: {sorted(missing)}"   # nothing lost
    assert env.r.get("trip:T-001:lastseq") == b"59"
    assert env.r.hget("trip:T-001:context", "stepIndex") == b"59"
    seqs = [json.loads(x)["seq"] for x in env.r.lrange("trip:T-001:recent", 0, -1)]
    assert seqs == list(range(59, 49, -1))


# ---------- CORS (app hosted on a different origin, e.g. Amplify) ----------

def test_cors_allows_configured_origin_only(env):
    import dataclasses
    s = dataclasses.replace(Settings(), cors_origins=("https://app.example",))
    app = create_app(service=env.service, s=s)
    with TestClient(app) as c:
        ok = c.options("/api/checkins", headers={
            "Origin": "https://app.example", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"})
        assert ok.headers.get("access-control-allow-origin") == "https://app.example"
        bad = c.options("/api/checkins", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        assert bad.headers.get("access-control-allow-origin") is None


# ---------- overload protection and metrics ----------

def test_sheds_load_with_503_and_retry_after_when_at_capacity(env, monkeypatch):
    """With room for 2 requests at once, a burst of 8 slow ones: 2 are handled, the rest
    are turned away straight away (not queued until the phone times out)."""
    import threading
    import time as _t
    s = dataclasses.replace(Settings(), reconciler_enabled=False, max_in_flight=2, retry_after_s=3)
    app = create_app(service=env.service, s=s)
    real = env.service.ingest_one
    monkeypatch.setattr(env.service, "ingest_one", lambda c: (_t.sleep(0.5), real(c))[1])
    results = []
    with TestClient(app) as client:
        def send(i):
            r = client.post("/api/checkins", json=checkin(100 + i))
            results.append((r.status_code, r.headers.get("retry-after"), r.json()))
        threads = [threading.Thread(target=send, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        codes = sorted(c for c, _, _ in results)
        assert codes.count(202) == 2 and codes.count(503) == 6
        shed = [r for r in results if r[0] == 503]
        assert all(ra == "3" and body["error"]["code"] == "overloaded" for _, ra, body in shed)
        # Shed check-ins were never stored, so the phone's retry is a normal first attempt.
        assert len(env.messages()) == 2
        metrics = client.get("/metrics").text
        assert "checkin_shed_total" in metrics and 'checkin_checkins_total{outcome="accepted"}' in metrics


def test_metrics_endpoint_reports_outcomes_and_lag(env):
    env.client.post("/api/checkins", json=checkin(1))
    env.client.post("/api/checkins", json=checkin(1))
    text = env.client.get("/metrics").text
    assert 'checkin_checkins_total{outcome="duplicate"}' in text
    assert 'checkin_delivery_lag_seconds_count{kind="live"}' in text
    assert 'checkin_http_requests_total{path="/api/checkins",status="202"}' in text
