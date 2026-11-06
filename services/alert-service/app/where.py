""""Where are you?" (issue 5.5): a caregiver asks, the traveller's phone shows the question and they reply.

One question per traveller at a time, in Redis (where:{travellerId}, contracts/redis-keys.md), kept for an hour.
Nothing can reach the phone until push notifications exist, so the traveller app checks for a question every few
seconds while a trip is open. Asking again while a question is waiting keeps that question, so the phone never shows
two. Asking and replying are Lua scripts, so two replicas, two caregivers or a phone retrying its reply can't
interleave: a question is answered once.
"""
import secrets

from app.incidents import now_iso

WHERE_TTL_S = 3600

# KEYS: where key   ARGV: requestId, askedBy, askedAt, ttl
# Returns 1 if a new question was made, 0 if one is already waiting.
_ASK = """
if redis.call('HGET', KEYS[1], 'status') == 'asked' then return 0 end
redis.call('DEL', KEYS[1])
redis.call('HSET', KEYS[1], 'requestId', ARGV[1], 'askedBy', ARGV[2], 'askedAt', ARGV[3], 'status', 'asked')
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[4]))
return 1
"""

# KEYS: where key   ARGV: requestId, answer, answeredAt, lat, lng
# Returns 1 if answered now, 2 if it was already answered, 0 if there's no such question (expired or replaced).
_REPLY = """
if redis.call('HGET', KEYS[1], 'requestId') ~= ARGV[1] then return 0 end
if redis.call('HGET', KEYS[1], 'status') ~= 'asked' then return 2 end
redis.call('HSET', KEYS[1], 'status', 'answered', 'answer', ARGV[2], 'answeredAt', ARGV[3], 'lat', ARGV[4], 'lng', ARGV[5])
return 1
"""

REPLY_OUTCOMES = {1: "answered", 2: "already", 0: "unknown"}


def where_key(traveller_id: str) -> str:
    return f"where:{traveller_id}"


def _number(value: str | None) -> float | None:
    return float(value) if value else None  # stored as "" when the phone had no GPS fix


class WhereRequests:
    def __init__(self, redis_client):
        self.r = redis_client
        self._ask = redis_client.register_script(_ASK)
        self._reply = redis_client.register_script(_REPLY)

    def get(self, traveller_id: str) -> dict | None:
        """The traveller's latest question, waiting or answered, or None."""
        raw = self.r.hgetall(where_key(traveller_id))
        if not raw:
            return None
        q = {k.decode(): v.decode() for k, v in raw.items()}
        return {
            "requestId": q["requestId"], "askedBy": q["askedBy"], "askedAt": q["askedAt"], "status": q["status"],
            "answer": q.get("answer") or None, "answeredAt": q.get("answeredAt") or None,
            "lat": _number(q.get("lat")), "lng": _number(q.get("lng")),
        }

    def ask(self, traveller_id: str, caregiver_id: str) -> tuple[dict, bool]:
        """(the question, whether it's new). A question already waiting is kept, not replaced."""
        created = self._ask(keys=[where_key(traveller_id)],
                            args=[f"W-{secrets.token_hex(4).upper()}", caregiver_id, now_iso(), WHERE_TTL_S])
        return self.get(traveller_id), bool(created)

    def reply(self, traveller_id: str, request_id: str, answer: str,
              lat: float | None, lng: float | None) -> tuple[dict | None, str]:
        """(the question, "answered" | "already" | "unknown")."""
        outcome = self._reply(keys=[where_key(traveller_id)],
                              args=[request_id, answer, now_iso(), "" if lat is None else lat, "" if lng is None else lng])
        return self.get(traveller_id), REPLY_OUTCOMES[outcome]
