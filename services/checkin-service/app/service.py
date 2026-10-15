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
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from .live_state import LiveState
from .models import Checkin, iso_z
from .queue import CheckinQueue
from .store import CheckinStore

log = logging.getLogger("checkin")


class Outcome(str, Enum):
    ACCEPTED = "accepted"      # new check-in, fully processed
    RECOVERED = "recovered"    # retry of one whose first attempt failed part-way; now finished
    DUPLICATE = "duplicate"    # already fully processed; ignored


@dataclass
class Counts:
    accepted: int = 0
    duplicates: int = 0

    def add(self, o: Outcome) -> None:
        if o is Outcome.DUPLICATE:
            self.duplicates += 1
        else:
            self.accepted += 1

    def body(self) -> dict:
        return {"accepted": self.accepted, "duplicates": self.duplicates}


class CheckinService:
    def __init__(self, store: CheckinStore, state: LiveState, queue: CheckinQueue):
        self.store = store
        self.state = state
        self.queue = queue

    def ingest(self, c: Checkin, now: datetime | None = None) -> Outcome:
        now = now or datetime.now(timezone.utc)
        result = self.store.put_new(c, received_at=iso_z(now))

        if not result.created and result.enqueued:
            return Outcome.DUPLICATE

        if result.created and not c.bufferedOffline:
            self.state.heartbeat(c.tripId, now, record_interval=True)

        self.state.apply(c)
        self.queue.send(c)
        self.store.mark_enqueued(c.tripId, c.seq)
        return Outcome.ACCEPTED if result.created else Outcome.RECOVERED

    def ingest_one(self, c: Checkin) -> Counts:
        counts = Counts()
        counts.add(self.ingest(c))
        if c.bufferedOffline:
            # Same reasoning as a batch: the phone is back, but this isn't a normal interval.
            self.state.heartbeat(c.tripId, datetime.now(timezone.utc), record_interval=False)
        return counts

    def ingest_batch(self, items: list[Checkin]) -> Counts:
        """Items arrive sorted oldest first. If one fails, the error propagates (503);
        the app resends the whole batch and the ones already done count as duplicates."""
        now = datetime.now(timezone.utc)
        counts = Counts()
        for c in items:
            counts.add(self.ingest(c, now=now))
        if items:
            # The phone is reachable again. Refresh lastseen so the Heartbeat Monitor
            # stops suspecting it, but don't record the offline gap as a normal interval.
            self.state.heartbeat(items[0].tripId, now, record_interval=False)
        return counts
