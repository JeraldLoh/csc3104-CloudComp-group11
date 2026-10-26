"""Live alerts: every incident change is published on Redis (alerts:events) and streamed to dashboards."""
import json
import time
from pathlib import Path
from urllib.parse import quote

import pytest
import redis
import redis.asyncio as redis_async
from fastapi.testclient import TestClient

from app.config import settings
from app.events import CHANNEL, AlertPublisher, build_event
from app.main import app
from app.stream import event_stream

ALERT_EVENT = json.loads((Path(__file__).resolve().parents[3] / "contracts" / "examples" / "alert-event.json").read_text())


@pytest.fixture
def client(dynamodb):
    return TestClient(app)


@pytest.fixture
def channel(redis_client):
    """Subscribed to alerts:events before the test acts, so no event is missed."""
    pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(CHANNEL)
    pubsub.get_message(timeout=1)  # the subscribe confirmation
    yield pubsub
    pubsub.close()


def next_event(pubsub, traveller_id: str, timeout_s: float = 5) -> dict:
    """The next event for this traveller (other tests and services may publish too)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        msg = pubsub.get_message(timeout=0.5)
        if msg:
            event = json.loads(msg["data"])
            if event["incident"]["travellerId"] == traveller_id:
                return event
    raise AssertionError(f"no event for {traveller_id} within {timeout_s} s")


# ---------------------------------------------------------------------------- the event
def test_event_has_the_contract_fields(report):
    incident = {**report, "incidentId": "U-042#T-001#off_route#2", "severity": "normal", "state": "open",
                "createdAt": "2026-10-12T08:15:31Z", "detectionCount": 1}
    event = build_event("opened", incident, "Alex")

    assert set(ALERT_EVENT) <= set(event)
    assert set(ALERT_EVENT["incident"]) <= set(event["incident"])
    assert event["event"] == "incident.opened"
    assert event["incident"]["message"] == "Alex may have gone off route."


def test_disruption_event_says_what_happened():
    incident = {"incidentId": "U-042#T-001#disruption#4", "travellerId": "U-042", "problemType": "disruption",
                "severity": "normal", "state": "open", "createdAt": "2026-10-12T08:05:13Z",
                "disruption": {"message": "No train service between Punggol and Sengkang"}}

    message = build_event("opened", incident)["incident"]["message"]

    assert message == "U-042's journey is affected: No train service between Punggol and Sengkang."


def test_publishing_never_fails_the_request(report):
    """Redis down: the incident is already saved, so the request must still succeed."""
    unreachable = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.5)
    incident = {**report, "incidentId": "x", "severity": "normal", "state": "open", "createdAt": "now"}

    AlertPublisher(unreachable).publish("opened", incident)  # must not raise


# ---------------------------------------------------------------------------- published on every change
def test_new_problem_publishes_incident_opened(client, channel, report):
    client.post("/internal/problems", json=report)

    event = next_event(channel, report["travellerId"])
    assert event["event"] == "incident.opened"
    assert event["incident"]["state"] == "open"
    assert event["incident"]["detectionCount"] == 1
    # so the dashboard can show "Trip T-001 · step 2" without reloading
    assert event["incident"]["tripId"] == "T-001"
    assert event["incident"]["stepIndex"] == 2


def test_repeat_problem_publishes_incident_updated(client, channel, report):
    client.post("/internal/problems", json=report)
    next_event(channel, report["travellerId"])
    client.post("/internal/problems", json=report)

    event = next_event(channel, report["travellerId"])
    assert event["event"] == "incident.updated"
    assert event["incident"]["detectionCount"] == 2


def test_ack_and_resolve_publish_their_events(client, channel, report, caregiver):
    iid = client.post("/internal/problems", json=report).json()["incidentId"]
    next_event(channel, report["travellerId"])

    client.post(f"/api/incidents/{quote(iid, safe='')}/ack", headers=caregiver)
    assert next_event(channel, report["travellerId"])["event"] == "incident.acknowledged"

    client.post(f"/api/incidents/{quote(iid, safe='')}/resolve", headers=caregiver)
    resolved = next_event(channel, report["travellerId"])
    assert resolved["event"] == "incident.resolved"
    assert resolved["incident"]["state"] == "resolved"


# ---------------------------------------------------------------------------- the stream (SSE)
class ConnectedRequest:
    """Stands in for the dashboard's request: never disconnects during the test."""

    async def is_disconnected(self) -> bool:
        return False


@pytest.fixture
async def async_redis():
    client = redis_async.Redis.from_url(settings.redis_url, decode_responses=True)
    yield client
    await client.aclose()


def event_for(traveller_id: str) -> str:
    incident = {"incidentId": f"{traveller_id}#T-001#off_route#2", "travellerId": traveller_id,
                "problemType": "off_route", "severity": "normal", "state": "open", "createdAt": "2026-10-12T08:15:31Z"}
    return json.dumps(build_event("opened", incident))


@pytest.mark.anyio
async def test_stream_delivers_published_events(async_redis):
    stream = event_stream(async_redis, ConnectedRequest(), traveller_id="U-stream-1", keepalive_s=5)
    try:
        assert await anext(stream) == ": connected\n\n"
        await async_redis.publish(CHANNEL, event_for("U-stream-1"))

        chunk = await anext(stream)
        assert chunk.startswith("data: ") and chunk.endswith("\n\n")
        assert json.loads(chunk[len("data: "):])["incident"]["travellerId"] == "U-stream-1"
    finally:
        await stream.aclose()


@pytest.mark.anyio
async def test_stream_only_sends_the_requested_travellers_events(async_redis):
    stream = event_stream(async_redis, ConnectedRequest(), traveller_id="U-stream-2", keepalive_s=5)
    try:
        await anext(stream)
        await async_redis.publish(CHANNEL, event_for("U-someone-else"))
        await async_redis.publish(CHANNEL, event_for("U-stream-2"))

        chunk = await anext(stream)
        assert json.loads(chunk[len("data: "):])["incident"]["travellerId"] == "U-stream-2"
    finally:
        await stream.aclose()


@pytest.mark.anyio
async def test_quiet_stream_sends_pings(async_redis):
    """So load balancers don't close an idle connection, and the dashboard can tell it's still alive."""
    stream = event_stream(async_redis, ConnectedRequest(), traveller_id="U-nobody", keepalive_s=0.3)
    try:
        await anext(stream)
        assert await anext(stream) == "event: ping\ndata: {}\n\n"
    finally:
        await stream.aclose()
