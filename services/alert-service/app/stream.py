"""GET /api/stream: Server-Sent Events (SSE) to a caregiver's dashboard.

The browser opens one long HTTP response (EventSource) and receives each alert-event.json as a
`data:` line the moment any replica publishes it. Each open stream subscribes to the Redis channel
itself, so closing the dashboard just closes that subscription.

A `ping` event goes out every 15 s when nothing else does, for two reasons:
- load balancers close connections that stay silent (the AWS ALB after 60 s);
- the dashboard can tell a quiet connection from a dead one. A proxy can keep the browser's side open
  after this end has gone (Vite's dev proxy does when the service restarts), so the page would show
  "Live" and receive nothing. If the dashboard hears no ping for a while, it reconnects.
Pings are a separate event type (`event: ping`), so code listening for alert events never sees them.

With a login, a caregiver only receives alerts for travellers they're linked to (issue 5.1). Their list of
travellers is re-read every 30 s, so a traveller added while the dashboard is open shows up without a reconnect.
"""
import asyncio
import json
import time
from typing import Callable

from app.events import CHANNEL

KEEPALIVE_S = 15
ALLOWED_REFRESH_S = 30
PING = "event: ping\ndata: {}\n\n"


async def event_stream(redis_async, request, traveller_id: str | None = None, keepalive_s: float = KEEPALIVE_S,
                       load_allowed: Callable[[], set[str]] | None = None):
    """Yields SSE chunks: alert events (only `traveller_id`'s, if given) and pings.

    `load_allowed()`: the traveller IDs this caller may see (None = no login, so everything)."""
    pubsub = redis_async.pubsub()
    await pubsub.subscribe(CHANNEL)
    try:
        allowed = await asyncio.to_thread(load_allowed) if load_allowed else None
        allowed_at = time.monotonic()
        yield ": connected\n\n"  # tells the browser (and tests) the subscription is live
        last_sent = time.monotonic()
        while not await request.is_disconnected():
            wait_s = max(0.0, keepalive_s - (time.monotonic() - last_sent))
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=wait_s)
            if msg is None:
                # None also means "a subscribe confirmation was skipped", so check the time really passed.
                if time.monotonic() - last_sent >= keepalive_s:
                    yield PING
                    last_sent = time.monotonic()
                continue
            data = msg["data"]
            event = json.loads(data)
            about = (event.get("incident") or event)["travellerId"]  # arrival events have no incident
            if traveller_id and about != traveller_id:
                continue
            if load_allowed:
                if time.monotonic() - allowed_at >= ALLOWED_REFRESH_S:
                    allowed = await asyncio.to_thread(load_allowed)  # DynamoDB call: keep it off the event loop
                    allowed_at = time.monotonic()
                if about not in allowed:
                    continue
            yield f"data: {data}\n\n"
            last_sent = time.monotonic()
    finally:
        await pubsub.unsubscribe(CHANNEL)
        await pubsub.aclose()
