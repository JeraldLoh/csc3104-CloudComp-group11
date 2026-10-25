"""GET /api/stream: Server-Sent Events (SSE) to a caregiver's dashboard.

The browser opens one long HTTP response (EventSource) and receives each alert-event.json as a
`data:` line the moment any replica publishes it. Each open stream subscribes to the Redis channel
itself, so closing the dashboard just closes that subscription.

A comment line goes out every 15 s when nothing else does: load balancers close connections that
stay silent (the AWS ALB after 60 s), and the browser's EventSource would then have to reconnect.
"""
import json
import time

from app.events import CHANNEL

KEEPALIVE_S = 15


async def event_stream(redis_async, request, traveller_id: str | None = None, keepalive_s: float = KEEPALIVE_S):
    """Yields SSE chunks: alert events (only `traveller_id`'s, if given) and keep-alive comments."""
    pubsub = redis_async.pubsub()
    await pubsub.subscribe(CHANNEL)
    try:
        yield ": connected\n\n"  # tells the browser (and tests) the subscription is live
        last_sent = time.monotonic()
        while not await request.is_disconnected():
            wait_s = max(0.0, keepalive_s - (time.monotonic() - last_sent))
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=wait_s)
            if msg is None:
                # None also means "a subscribe confirmation was skipped", so check the time really passed.
                if time.monotonic() - last_sent >= keepalive_s:
                    yield ": keep-alive\n\n"
                    last_sent = time.monotonic()
                continue
            data = msg["data"]
            if traveller_id and json.loads(data)["incident"]["travellerId"] != traveller_id:
                continue
            yield f"data: {data}\n\n"
            last_sent = time.monotonic()
    finally:
        await pubsub.unsubscribe(CHANNEL)
        await pubsub.aclose()
