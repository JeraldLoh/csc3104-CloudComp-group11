"""Redis keys the Check-in Service writes (contracts/redis-keys.md).

  trip:{tripId}:context    Hash   travellerId, stepIndex, routeVersion, status   -> Detectors, Coordinator
  trip:{tripId}:lastseq    String highest seq applied to context                  -> this service
  trip:{tripId}:recent     List   last 10 check-ins (JSON), newest first           -> Detectors
  hb:{tripId}:lastseen     String time the last live check-in arrived            -> Heartbeat Monitor
  hb:{tripId}:intervals    List   last 100 gaps between live arrivals, in ms      -> Heartbeat Monitor
  hb:{tripId}:status       String active | backgrounded                           -> Heartbeat Monitor

Two rules make this safe with several replicas and with offline batches:

1. Context only moves forward. A Lua script compares seq with lastseq and updates both
   in one atomic step, so an older check-in (from a batch or a slow replica) can never
   overwrite newer context.
2. Only live check-ins count as heartbeats. Buffered check-ins have old timestamps and
   arrive in a burst; adding them to the interval history would distort the gap
   distribution the Heartbeat Monitor's phi accrual detector depends on. A batch
   arriving does show the phone is back, so it refreshes lastseen without adding
   the long offline gap as an interval.

Null values (e.g. stepIndex unknown) are stored as "" because Redis hashes can't hold null.
"""
import json
from datetime import datetime, timezone

import redis

from .models import Checkin

# KEYS: lastseq, context, hbstatus
# ARGV: seq, travellerId, stepIndex, routeVersion, status, ttl, live(1/0)
# Returns 1 if context moved forward, 0 if this check-in was older than what's stored.
_ADVANCE_CONTEXT = """
local last = tonumber(redis.call('GET', KEYS[1]) or '-1')
local seq = tonumber(ARGV[1])
local ttl = tonumber(ARGV[6])
if seq <= last then
  redis.call('EXPIRE', KEYS[1], ttl)
  redis.call('EXPIRE', KEYS[2], ttl)
  return 0
end
redis.call('SET', KEYS[1], seq, 'EX', ttl)
redis.call('HSET', KEYS[2], 'travellerId', ARGV[2], 'stepIndex', ARGV[3],
           'routeVersion', ARGV[4], 'status', ARGV[5])
redis.call('EXPIRE', KEYS[2], ttl)
if ARGV[7] == '1' then
  redis.call('SET', KEYS[3], ARGV[5], 'EX', ttl)
end
return 1
"""

# KEYS: recent   ARGV: checkin json, seq, max length, ttl
# Inserts in seq order (newest first) and skips a seq already present, so offline
# check-ins land in the right place and retries don't create duplicates.
_INSERT_RECENT = """
local items = redis.call('LRANGE', KEYS[1], 0, -1)
local seq = tonumber(ARGV[2])
local maxlen = tonumber(ARGV[3])
local out = {}
local placed = false
for _, raw in ipairs(items) do
  local s = tonumber(cjson.decode(raw)['seq'])
  if s == seq then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[4]))
    return 0
  end
  if (not placed) and seq > s then
    table.insert(out, ARGV[1])
    placed = true
  end
  table.insert(out, raw)
end
if not placed then table.insert(out, ARGV[1]) end
redis.call('DEL', KEYS[1])
for i = 1, math.min(#out, maxlen) do redis.call('RPUSH', KEYS[1], out[i]) end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[4]))
return 1
"""


def _s(v) -> str:
    return "" if v is None else str(v)


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class LiveState:
    def __init__(self, client: redis.Redis, ttl_s: int, recent_max: int, intervals_max: int):
        self.r = client
        self.ttl = ttl_s
        self.recent_max = recent_max
        self.intervals_max = intervals_max
        self._advance = client.register_script(_ADVANCE_CONTEXT)
        self._insert_recent = client.register_script(_INSERT_RECENT)

    def apply(self, c: Checkin) -> bool:
        """Update context and recent list. Safe to repeat. Returns True if context moved forward."""
        t = c.tripId
        live = "0" if c.bufferedOffline else "1"
        advanced = self._advance(
            keys=[f"trip:{t}:lastseq", f"trip:{t}:context", f"hb:{t}:status"],
            args=[c.seq, c.travellerId, _s(c.stepIndex), _s(c.routeVersion), c.status, self.ttl, live],
        )
        self._insert_recent(
            keys=[f"trip:{t}:recent"],
            args=[json.dumps(c.to_contract()), c.seq, self.recent_max, self.ttl],
        )
        return bool(advanced)

    def heartbeat(self, trip_id: str, now: datetime, record_interval: bool) -> int | None:
        """Mark the phone as seen now. Returns the gap in ms that was recorded, if any.

        SET ... GET swaps in the new time and returns the old one in a single step,
        so two replicas can't both read the same old value and record the same gap twice.
        """
        now_iso = now.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        prev = self.r.set(f"hb:{trip_id}:lastseen", now_iso, ex=self.ttl, get=True)
        if not record_interval or prev is None:
            return None
        prev_s = prev.decode() if isinstance(prev, bytes) else prev
        gap_ms = max(0, int((now - _parse_iso(prev_s)).total_seconds() * 1000))
        key = f"hb:{trip_id}:intervals"
        pipe = self.r.pipeline()
        pipe.lpush(key, gap_ms)
        pipe.ltrim(key, 0, self.intervals_max - 1)
        pipe.expire(key, self.ttl)
        pipe.execute()
        return gap_ms

    def ping(self) -> None:
        self.r.ping()
