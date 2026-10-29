"""How one check-in is processed.

    1. DynamoDB conditional put (tripId + seq)   new or duplicate?
    2. Redis heartbeat                            live check-ins only
    3. Redis context + recent                     before the queue, so when the Coordinator
                                                  picks the message up, the detectors can
                                                  already see this check-in in Redis
    4. SQS send                                   to the Coordinator
    5. DynamoDB enqueued = true                   this check-in is fully handled

If anything fails between 1 and 5, the request returns 503 and the item is left with
enqueued = false. The app retries (it keeps the check-in in its outbox until it gets a
202). The retry hits the duplicate path, sees enqueued = false, and finishes steps 3-5.
Steps 3-5 are safe to repeat, so a check-in is never lost and never needs manual repair.

The cost is that the Coordinator may occasionally get the same check-in twice (at-least-once
delivery). contracts/queues.md already requires it to ignore repeated tripId + seq.

If the phone never retries (its battery died right after a failed attempt), the clean-up
task (reconciler.py) finds the stuck item and finishes steps 3-5 for it instead.
"""
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from . import metrics
from .live_state import LiveState
from .models import Checkin, iso_ms
from .queue import CheckinQueue
from .store import CheckinStore, item_to_checkin

log = logging.getLogger("checkin")


class Outcome(str, Enum):
    ACCEPTED = "accepted"      # new check-in, fully processed
    RECOVERED = "recovered"    # retry of one whose first attempt failed part-way; now finished
    DUPLICATE = "duplicate"    # already fully processed; ignored


@dataclass
class Counts:
    accepted: int = 0
    duplicates: int = 0
    processed: int = 0   # how many of the request's check-ins (oldest first) were handled

    def add(self, o: Outcome) -> None:
        self.processed += 1
        if o is Outcome.DUPLICATE:
            self.duplicates += 1
        else:
            self.accepted += 1

    def body(self) -> dict:
        return {"accepted": self.accepted, "duplicates": self.duplicates, "processed": self.processed}


class CheckinService:
    def __init__(self, store: CheckinStore, state: LiveState, queue: CheckinQueue):
        self.store = store
        self.state = state
        self.queue = queue

    def ingest(self, c: Checkin, now: datetime | None = None, live: bool = True) -> Outcome:
        """`live`: this check-in arrived on its own, in the phone's normal rhythm, so its
        arrival counts as a heartbeat interval. Batches pass live=False (see ingest_batch)."""
        now = now or datetime.now(timezone.utc)
        result = self.store.put_new(c, received_at=iso_ms(now))

        if not result.created and result.enqueued:
            metrics.CHECKINS.labels("duplicate").inc()
            return Outcome.DUPLICATE

        if live and result.created and not c.bufferedOffline:
            self.state.heartbeat(c.tripId, now, record_interval=True)

        self.finish(c)
        outcome = Outcome.ACCEPTED if result.created else Outcome.RECOVERED
        metrics.CHECKINS.labels(outcome.value).inc()
        if result.created:
            lag = (now - c.timestamp).total_seconds()
            metrics.DELIVERY_LAG.labels("buffered" if c.bufferedOffline else "live").observe(max(0.0, lag))
        return outcome

    def finish(self, c: Checkin) -> None:
        """Steps 3-5. Each is safe to repeat, so a retry, the clean-up task, or both at
        once can run this for the same check-in without harm (at worst the Coordinator
        receives a second copy, which it ignores)."""
        self.state.apply(c)
        self.queue.send(c)
        self.store.mark_enqueued(c.tripId, c.seq)

    def reconcile_once(self, grace_s: float, limit: int = 100, now: datetime | None = None) -> int:
        """Finish check-ins stored more than `grace_s` ago that never reached the queue.
        The grace period leaves in-flight requests and quick phone retries to finish
        their own work first. Returns how many were finished."""
        now = now or datetime.now(timezone.utc)
        cutoff = iso_ms(now - timedelta(seconds=grace_s))
        done = 0
        for candidate in self.store.find_stuck(cutoff, limit):
            # The index lags slightly behind the table: re-read before acting.
            item = self.store.get(candidate["tripId"], int(candidate["seq"]))
            if not item or item.get("enqueued"):
                continue
            c = item_to_checkin(item)
            self.finish(c)
            metrics.RECONCILED.inc()
            done += 1
            log.info("reconciled stuck check-in %s#%s", c.tripId, c.seq)
        return done

    def ingest_one(self, c: Checkin) -> Counts:
        counts = Counts()
        counts.add(self.ingest(c))
        if c.bufferedOffline:
            # Same reasoning as a batch: the phone is back, but this isn't a normal interval.
            self.state.heartbeat(c.tripId, datetime.now(timezone.utc), record_interval=False)
        return counts

    def ingest_batch(self, items: list[Checkin], budget_s: float | None = None) -> Counts:
        """Items arrive sorted oldest first. If one fails, the error propagates (503);
        the app resends the whole batch and the ones already done count as duplicates.

        Time budget: stop starting new items once `budget_s` has passed (always doing at
        least one) and report how many were processed; the app removes exactly those and
        sends the rest next. Without this, a big catch-up batch on a busy server can take
        longer than the phone's timeout: the phone resends the same batch while the server
        is still working on the abandoned one, and every phone does the same, so the
        server stays busy but no phone ever gets an answer (a retry storm that sustains
        itself). With it, every request answers in about `budget_s` and always makes progress."""
        now = datetime.now(timezone.utc)
        start = time.monotonic()
        counts = Counts()
        for c in items:
            if budget_s is not None and counts.processed > 0 and time.monotonic() - start >= budget_s:
                break
            # A batch means the phone was behind (offline, or waiting out a backoff); it can
            # also hold fresh check-ins made during that wait. They all arrive at the same
            # instant, so counting them as heartbeats would record gaps of 0 ms and distort
            # the interval history the failure detector learns from. Only lastseen is
            # refreshed below.
            counts.add(self.ingest(c, now=now, live=False))
        if counts.processed:
            # The phone is reachable again. Refresh lastseen so the Heartbeat Monitor
            # stops suspecting it, but don't record the offline gap as a normal interval.
            self.state.heartbeat(items[0].tripId, now, record_interval=False)
        return counts
