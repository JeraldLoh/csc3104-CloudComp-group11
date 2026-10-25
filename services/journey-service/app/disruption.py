"""The disruption checker: what the Lambda runs (see lambda_handler.py). It is deliberately separate from the API
code (journey_client.py): it needs only the LTA client, Redis and the settings, so the Lambda package stays small.

For every trip in the Redis set `active-trips`: `trip:{id}:route` has the steps (train steps carry `stops`) and
`trip:{id}:context` has travellerId and the current stepIndex (contracts/redis-keys.md). A train step the
traveller hasn't finished whose stations overlap an LTA TrainServiceAlerts segment produces one disruption.json
(contracts/examples/disruption.json), passed to `send` (the `careroute-disruptions` queue).

Delivery is at-least-once: the message is sent first and the "already sent" marker is written afterwards, so a
crash in between can only cause a duplicate (receivers handle those, see contracts/queues.md), never a missed alert.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from .config import Settings
from .lta import SST, LtaClient

log = logging.getLogger("journey.disruption")


def _text(value: Any) -> Any:
    """Redis returns bytes unless the client was made with decode_responses=True."""
    return value.decode() if isinstance(value, bytes) else value


class DisruptionChecker:
    def __init__(
        self,
        redis_client,
        lta: LtaClient,
        settings: Settings,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        if redis_client is None:
            raise RuntimeError("the disruption checker needs a Redis client")
        self._r = redis_client
        self._lta = lta
        self._s = settings
        self._clock = clock

    def check(self, send: Callable[[dict], None], dry_run: bool = False, ignore_sent: bool = False) -> dict:
        """One pass over all active trips. Returns counts for logging/metrics.

        dry_run: call `send` but leave no trace (no "already sent" marker), so a real run afterwards still sends.
        ignore_sent: report disruptions even if they were sent before (used by the demo tool's --again)."""
        summary = {"activeTrips": 0, "disruptions": 0, "sent": 0, "duplicates": 0, "skipped": 0}
        alerts = self._lta.train_alerts()
        if alerts["status"] < self._s.disruption_min_status or not alerts["segments"]:
            return summary

        trip_ids = sorted(_text(t) for t in self._r.smembers("active-trips"))
        pipe = self._r.pipeline(transaction=False)            # every trip's route and context in one round trip
        for trip_id in trip_ids:
            pipe.get(f"trip:{trip_id}:route")
            pipe.hgetall(f"trip:{trip_id}:context")
        fetched = pipe.execute() if trip_ids else []

        for n, trip_id in enumerate(trip_ids):
            summary["activeTrips"] += 1
            route = self._parse_route(fetched[2 * n])
            traveller_id, current_step = self._parse_context(fetched[2 * n + 1], route)
            if route is None or traveller_id is None:
                log.warning("trip %s: missing route or travellerId, skipped", trip_id)
                summary["skipped"] += 1
                continue
            for event in self._events_for_trip(trip_id, traveller_id, current_step, route, alerts):
                summary["disruptions"] += 1
                marker = f"journey:disruption-sent:{event['disruptionId']}:{trip_id}"
                if not ignore_sent and self._r.exists(marker):
                    summary["duplicates"] += 1
                    continue
                send(event)                                         # raises -> no marker -> retried next run
                if not dry_run:
                    self._r.set(marker, "1", ex=self._s.disruption_dedupe_ttl_s)
                summary["sent"] += 1
                log.info("disruption %s sent for trip %s", event["disruptionId"], trip_id)
        return summary

    @staticmethod
    def _parse_route(raw) -> dict | None:
        """The value of `trip:{id}:route`."""
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    @staticmethod
    def _parse_context(raw: dict, route: dict | None) -> tuple[str | None, int]:
        """The hash `trip:{id}:context`: (travellerId, the current stepIndex)."""
        ctx = {_text(k): _text(v) for k, v in (raw or {}).items()}
        traveller = ctx.get("travellerId") or (route or {}).get("travellerId")
        try:
            step = int(ctx.get("stepIndex", 0))
        except ValueError:
            step = 0
        return traveller, step

    def _events_for_trip(self, trip_id, traveller_id, current_step, route, alerts) -> list[dict]:
        events = []
        for seg in alerts["segments"]:
            affected = set(seg["stations"])
            for step in route.get("steps", []):
                if step.get("mode") != "train" or step.get("stepIndex", 0) < current_step:
                    continue
                stops = step.get("stops") or [step.get("from") or {}, step.get("to") or {}]
                hit = [s for s in stops if s.get("code") in affected]
                if not hit:
                    continue
                events.append(self._event(trip_id, traveller_id, step["stepIndex"], seg, hit, alerts))
                break                   # one disruption per trip per affected segment: the first step it hits
        return events

    def _event(self, trip_id, traveller_id, step_index, seg, hit, alerts) -> dict:
        now = self._clock()
        messages = alerts["messages"]
        created = self._parse_created(messages[0]["createdDate"]) if messages else None
        # Stable while LTA's message is unchanged, so repeated runs give the same id. Without a
        # message only the date is used, so two outages on one line in one day share an id.
        stamp = (created.astimezone(timezone.utc).strftime("%Y%m%d-%H%M") if created
                 else now.astimezone(timezone.utc).strftime("%Y%m%d-0000"))
        return {
            "disruptionId": f"D-{seg['line']}-{stamp}",
            "source": "lta-datamall",
            "tripId": trip_id,
            "travellerId": traveller_id,
            "affectedStepIndex": step_index,
            "mode": "train",
            "line": seg["line"],
            "affectedStations": [s.get("name") or s["code"] for s in hit],
            "message": messages[0]["content"] if messages else f"Train disruption on {seg['line']}",
            "detectedAt": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    @staticmethod
    def _parse_created(value: str | None) -> datetime | None:
        """LTA writes CreatedDate in Singapore time, e.g. '2026-10-21 09:33:49'."""
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SST)
        except (TypeError, ValueError):
            return None
