"""SqsConsumer with an in-memory SQS and a fake pipeline: delete only after success."""
import asyncio
import json
import time


from app.consumer import SqsConsumer


class FakeSqs:
    """Hands out queued messages; records deletes. Empty receives pause briefly like long polling."""

    def __init__(self, bodies=(), receive_errors=0, delete_fails=False):
        self.messages = [{"MessageId": f"m{i}", "ReceiptHandle": f"r{i}",
                          "Body": b if isinstance(b, str) else json.dumps(b)} for i, b in enumerate(bodies)]
        self.deleted = []
        self.receive_errors = receive_errors
        self.receives = 0
        self.delete_fails = delete_fails

    def receive_message(self, QueueUrl, MaxNumberOfMessages, WaitTimeSeconds):
        self.receives += 1
        if self.receive_errors:
            self.receive_errors -= 1
            raise ConnectionError("sqs unreachable")
        batch, self.messages = self.messages[:MaxNumberOfMessages], self.messages[MaxNumberOfMessages:]
        if not batch:
            time.sleep(0.01)
        return {"Messages": batch} if batch else {}

    def delete_message(self, QueueUrl, ReceiptHandle):
        if self.delete_fails:
            raise ConnectionError("sqs unreachable")
        self.deleted.append(ReceiptHandle)


class FakePipeline:
    """seq -> "ok" | "fail" | seconds to take."""

    def __init__(self, behaviour=None):
        self.behaviour = behaviour or {}
        self.processed = []

    async def process(self, checkin):
        b = self.behaviour.get(checkin["seq"], "ok")
        if b == "fail":
            raise RuntimeError("aggregator down")
        if isinstance(b, (int, float)):
            await asyncio.sleep(b)
        self.processed.append(checkin["seq"])


def checkin(seq):
    return {"tripId": "T-001", "travellerId": "U-042", "seq": seq}


def consumer(sqs, pipeline, **kw) -> SqsConsumer:
    return SqsConsumer(sqs, "http://sqs/careroute-checkins", pipeline, error_backoff_s=0.01, **kw)


# --- delete only after success --------------------------------------------------------------

def test_processed_message_is_deleted():
    sqs, pipe = FakeSqs([checkin(1)]), FakePipeline()
    assert asyncio.run(consumer(sqs, pipe).poll_once()) == 1
    assert pipe.processed == [1]
    assert sqs.deleted == ["r0"]


def test_failed_message_is_not_deleted():
    sqs, pipe = FakeSqs([checkin(1)]), FakePipeline({1: "fail"})
    assert asyncio.run(consumer(sqs, pipe).poll_once()) == 0
    assert sqs.deleted == []


def test_bad_json_is_left_for_the_dead_letter_queue():
    sqs = FakeSqs(["{not json"])
    assert asyncio.run(consumer(sqs, FakePipeline()).poll_once()) == 0
    assert sqs.deleted == []


def test_one_failure_does_not_affect_the_others():
    sqs, pipe = FakeSqs([checkin(1), checkin(2), checkin(3)]), FakePipeline({2: "fail"})
    assert asyncio.run(consumer(sqs, pipe).poll_once()) == 2
    assert sorted(sqs.deleted) == ["r0", "r2"]


def test_delete_failure_is_reported_not_raised():
    sqs = FakeSqs([checkin(1)], delete_fails=True)
    assert asyncio.run(consumer(sqs, FakePipeline()).poll_once()) == 0


# --- batches -------------------------------------------------------------------------------------

def test_receives_at_most_max_messages():
    sqs = FakeSqs([checkin(i) for i in range(5)])
    assert asyncio.run(consumer(sqs, FakePipeline(), max_messages=3).poll_once()) == 3
    assert len(sqs.messages) == 2


def test_checkins_in_a_batch_are_analysed_concurrently():
    sqs = FakeSqs([checkin(i) for i in range(5)])
    pipe = FakePipeline({i: 0.1 for i in range(5)})
    start = time.perf_counter()
    asyncio.run(consumer(sqs, pipe).poll_once())
    assert time.perf_counter() - start < 0.3   # ~0.1 s together, not 0.5 s


# --- the loop keeps going ---------------------------------------------------------------------------

def run_for(c: SqsConsumer, seconds: float):
    async def go():
        c.start()
        await asyncio.sleep(seconds)
        await c.stop()
    asyncio.run(go())


def test_loop_drains_the_queue_across_receives():
    sqs, pipe = FakeSqs([checkin(i) for i in range(25)]), FakePipeline()
    run_for(consumer(sqs, pipe), 0.3)
    assert sorted(pipe.processed) == list(range(25))
    assert len(sqs.deleted) == 25


def test_loop_survives_sqs_errors():
    sqs, pipe = FakeSqs([checkin(1)], receive_errors=3), FakePipeline()
    run_for(consumer(sqs, pipe), 0.3)
    assert pipe.processed == [1]
    assert sqs.receives > 3


def test_stop_ends_the_loop():
    c = consumer(FakeSqs(), FakePipeline())
    run_for(c, 0.05)
    assert c._task.done()


def test_stop_before_start_is_harmless():
    asyncio.run(consumer(FakeSqs(), FakePipeline()).stop())
