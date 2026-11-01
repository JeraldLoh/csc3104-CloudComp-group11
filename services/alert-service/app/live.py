"""Where travellers are right now (issue 5.5), from what the Check-in Service keeps in Redis
(contracts/redis-keys.md): trip:{tripId}:context, trip:{tripId}:recent and hb:{tripId}:lastseen.

Nothing says which trip a traveller is on: `active-trips` only lists trip IDs, and only for trips started in the
Journey Service. So each context is read to see whose it is. That walks every trip with a recent check-in, which is
fine at this project's scale and only happens when a dashboard asks. A trip stays here until its last check-in is
older than the window, which keeps a phone that has gone quiet on the map. A traveller who pressed "I've arrived"
has `arrivedAt` set (trip:{tripId}:arrived); a trip the caregiver ended without that has no marker.
"""
import json
from datetime import datetime, timedelta, timezone

ARRIVED_TTL_S = 24 * 3600  # as long as the Check-in Service keeps a trip's keys


def arrived_key(trip_id: str) -> str:
    return f"trip:{trip_id}:arrived"


def _parse_time(value: bytes | None) -> datetime | None:
    return datetime.fromisoformat(value.decode().replace("Z", "+00:00")) if value else None


class LiveTrips:
    def __init__(self, redis_client, window_s: int):
        self.r = redis_client
        self.window = timedelta(seconds=window_s)

    def for_travellers(self, traveller_ids: list[str], now: datetime | None = None) -> dict[str, dict]:
        """{travellerId: their latest trip}, leaving out travellers with nothing recent."""
        wanted = set(traveller_ids)
        keys = list(self.r.scan_iter(match="trip:*:context", count=500)) if wanted else []
        if not keys:
            return {}
        pipe = self.r.pipeline(transaction=False)
        for key in keys:
            pipe.hgetall(key)
        contexts = [{k.decode(): v.decode() for k, v in c.items()} for c in pipe.execute()]
        mine = [(key.decode().split(":")[1], c) for key, c in zip(keys, contexts) if c.get("travellerId") in wanted]
        if not mine:
            return {}

        pipe = self.r.pipeline(transaction=False)
        for trip_id, _ in mine:
            pipe.get(f"hb:{trip_id}:lastseen")
            pipe.lrange(f"trip:{trip_id}:recent", 0, -1)
            pipe.get(arrived_key(trip_id))
        replies = pipe.execute()

        oldest = (now or datetime.now(timezone.utc)) - self.window
        found: dict[str, dict] = {}
        for i, (trip_id, context) in enumerate(mine):
            seen_raw, recent, arrived = replies[3 * i:3 * i + 3]
            last_seen = _parse_time(seen_raw)
            if not last_seen or last_seen < oldest or not recent:
                continue
            traveller_id = context["travellerId"]
            if traveller_id not in found or last_seen > found[traveller_id]["seen"]:
                found[traveller_id] = {"seen": last_seen, **self._trip(trip_id, context, recent, seen_raw, arrived)}
        return {traveller_id: {k: v for k, v in trip.items() if k != "seen"} for traveller_id, trip in found.items()}

    @staticmethod
    def _trip(trip_id: str, context: dict, recent: list[bytes], last_seen: bytes, arrived: bytes | None) -> dict:
        checkins = [json.loads(raw) for raw in recent]  # newest first
        latest = checkins[0]
        step = context.get("stepIndex")
        return {
            "tripId": trip_id,
            "stepIndex": int(step) if step else None,  # stored as "" when a check-in didn't say
            "status": context.get("status"),
            "lastSeenAt": last_seen.decode(),
            "arrivedAt": arrived.decode() if arrived else None,
            "lat": latest["lat"],
            "lng": latest["lng"],
            "accuracyM": latest.get("accuracyM"),
            "batteryPct": latest.get("batteryPct"),
            "trail": [[c["lat"], c["lng"]] for c in reversed(checkins)],  # oldest first, as a line is drawn
        }
