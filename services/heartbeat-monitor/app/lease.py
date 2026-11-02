"""Leader lease: only one replica scans the active trips at a time.

Same pattern as the Check-in Service's clean-up task (services/checkin-service/app/reconciler.py):

    SET lock:heartbeat-monitor <its id> NX PX <ttl>      take it if nobody holds it
    (Lua) if GET == <its id> then PEXPIRE                 renew only if still ours

If the leader dies it stops renewing, and another replica takes over once the lease expires. A normal
shutdown releases it straight away (Lua compare-and-delete), so a rolling update hands over at once.

Two leaders at once (a paused leader waking up after its lease ran out) can't send two alerts for one
silence that matter: the Alert Service turns repeated reports of the same problem into one incident
(incident ID = traveller#trip#no_response#step). The lease saves work; it isn't needed for correctness.
"""
import os
import socket
import uuid

import redis

LEASE_KEY = "lock:heartbeat-monitor"

_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def replica_id() -> str:
    """Pod name (Kubernetes sets HOSTNAME) plus a random suffix, unique per process."""
    return f"{os.getenv('HOSTNAME') or socket.gethostname()}-{uuid.uuid4().hex[:6]}"


class LeaderLease:
    def __init__(self, client: redis.Redis, holder: str, ttl_ms: int, key: str = LEASE_KEY):
        self.r = client
        self.holder = holder
        self.ttl_ms = ttl_ms
        self.key = key
        self._renew = client.register_script(_RENEW)
        self._release = client.register_script(_RELEASE)

    def acquire_or_renew(self) -> bool:
        """True if this replica holds the lease after the call."""
        if self._renew(keys=[self.key], args=[self.holder, self.ttl_ms]):
            return True
        return bool(self.r.set(self.key, self.holder, nx=True, px=self.ttl_ms))

    def release(self) -> None:
        self._release(keys=[self.key], args=[self.holder])
