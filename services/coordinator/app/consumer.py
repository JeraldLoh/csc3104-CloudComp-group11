"""Reads careroute-checkins (contracts/queues.md) and analyses each check-in.

A message is deleted only after the analysis finished. If it fails, or this replica dies
part-way, the message becomes visible again after the queue's visibility timeout (30 s)
and any replica retries it; after 5 failed attempts SQS moves it to the dead-letter queue.

boto3 is blocking, so its calls run in a worker thread and the event loop stays free for
the detector calls of other check-ins.
"""
import asyncio
import json
import logging

from .pipeline import Pipeline

log = logging.getLogger("coordinator.consumer")


class SqsConsumer:
    def __init__(self, sqs_client, queue_url: str, pipeline: Pipeline, max_messages: int = 10,
                 wait_s: int = 20, error_backoff_s: float = 2.0):
        self.sqs = sqs_client
        self.queue_url = queue_url
        self.pipeline = pipeline
        self.max_messages = max_messages
        self.wait_s = wait_s
        self.error_backoff_s = error_backoff_s
        self._task: asyncio.Task | None = None

    def _receive(self) -> list[dict]:
        resp = self.sqs.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=self.max_messages,
            WaitTimeSeconds=self.wait_s,   # long polling: one request waits up to 20 s
        )
        return resp.get("Messages", [])

    def _delete(self, receipt_handle: str) -> None:
        self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt_handle)

    async def handle(self, message: dict) -> bool:
        """True if the check-in was analysed and its message deleted."""
        try:
            checkin = json.loads(message["Body"])
            await self.pipeline.process(checkin)
        except Exception as e:  # noqa: BLE001 - leave it on the queue for a retry
            log.warning("message %s not processed, it will be retried: %s: %s",
                        message.get("MessageId"), type(e).__name__, e)
            return False
        try:
            await asyncio.to_thread(self._delete, message["ReceiptHandle"])
        except Exception as e:  # noqa: BLE001 - redelivery is harmless: it's deduplicated
            log.warning("couldn't delete message %s (it will come back and be skipped): %s",
                        message.get("MessageId"), e)
            return False
        return True

    async def poll_once(self) -> int:
        """One receive; check-ins in it are analysed concurrently. Returns how many were deleted."""
        messages = await asyncio.to_thread(self._receive)
        results = await asyncio.gather(*(self.handle(m) for m in messages))
        return sum(results)

    async def run(self) -> None:
        log.info("consuming %s", self.queue_url)
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - SQS hiccup: back off, keep consuming
                log.warning("receive failed: %s", e)
                await asyncio.sleep(self.error_backoff_s)

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="sqs-consumer")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
