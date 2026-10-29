"""Background clean-up task ("reconciler") and the leader lease that coordinates it.

Why it exists
-------------
A check-in is finished in five steps (service.py). If step 4 or 5 fails, the item stays
in DynamoDB with `pending` set and the phone is told to retry. Normally the retry
finishes it. But if the phone never retries (battery died, app uninstalled), nothing
would ever move that check-in to the queue, and the detectors would never see it.
Every RECONCILE_INTERVAL_S seconds this task finds such items (older than a grace
period, so normal in-flight requests aren't touched) and finishes them.

Only one replica runs it at a time
----------------------------------
All replicas share one Redis. Each tick, a replica tries to take or renew a lease:

    SET lock:checkin-reconciler <its id> NX PX <ttl>       take it if nobody holds it
    (Lua) if GET == <its id> then PEXPIRE                    renew only if still ours

The holder is the leader and runs the clean-up. If it crashes, it stops renewing and
the lease expires after `ttl`, so another replica takes over within about ttl + one
interval. Leaving releases it straight away (Lua compare-and-delete), so a normal
shutdown or rolling update hands over immediately.

The lease is for efficiency, not correctness
--------------------------------------------
Leases can overlap: a leader paused longer than the ttl (a long GC pause, a frozen
container) can wake up still believing it leads while another replica has taken over.
Rather than relying on the lock never overlapping (which no lease can guarantee without
fencing tokens), finishing a check-in is idempotent: the Redis update only moves forward,
`mark_enqueued` can be repeated, and the Coordinator ignores repeated tripId + seq. Two
leaders at once therefore cost at most a duplicate queue message, never a wrong result.
(Kleppmann, "How to do distributed locking", 2016.)
"""
import asyncio
import logging
import os
import socket
import uuid

import redis
from starlette.concurrency import run_in_threadpool

from . import metrics
from .service import CheckinService

log = logging.getLogger("checkin.reconciler")

LEASE_KEY = "lock:checkin-reconciler"

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

    def current_holder(self) -> str | None:
        v = self.r.get(self.key)
        return v.decode() if isinstance(v, bytes) else v


class Reconciler:
    def __init__(self, service: CheckinService, lease: LeaderLease, interval_s: float, grace_s: float, batch: int):
        self.service = service
        self.lease = lease
        self.interval_s = interval_s
        self.grace_s = grace_s
        self.batch = batch
        self.is_leader = False
        self.total_reconciled = 0
        self._task: asyncio.Task | None = None

    def tick(self) -> int:
        """One round: take/renew the lease; if leader, finish stuck check-ins. Blocking."""
        try:
            self.is_leader = self.lease.acquire_or_renew()
        except redis.RedisError as e:
            # Can't tell who leads, and finishing needs Redis anyway: skip this round.
            log.warning("lease check failed: %s", e)
            self.is_leader = False
            return 0
        metrics.LEADER.set(1 if self.is_leader else 0)
        if not self.is_leader:
            return 0
        done = 0
        try:
            # Keep going while full batches come back, so a backlog clears in one round.
            while True:
                n = self.service.reconcile_once(self.grace_s, self.batch)
                done += n
                if n < self.batch:
                    break
        except Exception as e:  # noqa: BLE001 - a bad round must not kill the loop
            log.warning("clean-up round failed (will retry next round): %s", e)
        self.total_reconciled += done
        return done

    async def run(self) -> None:
        while True:
            await run_in_threadpool(self.tick)
            await asyncio.sleep(self.interval_s)

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="checkin-reconciler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        try:
            await run_in_threadpool(self.lease.release)  # hand over straight away
        except redis.RedisError:
            pass  # it will expire on its own
