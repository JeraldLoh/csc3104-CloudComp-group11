"""Reads the careroute-disruptions queue (contracts/queues.md) and turns each message into an incident.

Every replica runs this loop in a background thread. SQS gives each message to one replica at a time.
A message is deleted only after its incident is saved: if a replica crashes half-way, the message
becomes visible again after the visibility timeout (60 s) and another replica handles it. Standard
queues can also deliver a message twice; the incident ID (…#disruption#<step>) makes that harmless.
A message that keeps failing moves to careroute-disruptions-dlq after 5 attempts.
"""
import logging
import threading

from pydantic import ValidationError

from app.incidents import IncidentStore
from app.models import Disruption

log = logging.getLogger("alert.disruptions")


class DisruptionConsumer:
    def __init__(self, sqs_client, queue_url: str, incidents: IncidentStore, wait_s: int = 20):
        self.sqs = sqs_client
        self.queue_url = queue_url
        self.incidents = incidents
        self.wait_s = wait_s  # long polling: wait up to this long for a message instead of busy-looping
        self._stop = threading.Event()

    def handle(self, body: str) -> bool:
        """Save the incident for one message. Returns True if the message can be deleted."""
        try:
            disruption = Disruption.model_validate_json(body)
        except ValidationError as e:
            # Retrying won't fix it: leave it, and after 5 attempts SQS moves it to the dead-letter queue.
            log.error("unreadable disruption message, leaving it for the dead-letter queue: %s", e)
            return False
        incident, outcome = self.incidents.record_problem(
            disruption.as_problem(), details={"disruption": disruption.details()}
        )
        log.info("%s %s from %s", outcome, incident["incidentId"], disruption.disruptionId)
        return True

    def poll_once(self) -> int:
        """Receive up to 10 messages, handle them, and delete the ones that were saved. Returns that number."""
        resp = self.sqs.receive_message(QueueUrl=self.queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=self.wait_s)
        saved = 0
        for msg in resp.get("Messages", []):
            try:
                ok = self.handle(msg["Body"])
            except Exception:
                # e.g. DynamoDB unreachable. Not deleting it means SQS offers it again in 60 s.
                log.exception("couldn't save disruption message; it will be retried")
                ok = False
            if ok:
                self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=msg["ReceiptHandle"])
                saved += 1
        return saved

    def run(self) -> None:
        log.info("reading disruptions from %s", self.queue_url)
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("receiving from the disruptions queue failed; retrying in 5 s")
                self._stop.wait(5)

    def start(self) -> None:
        # Daemon thread: it doesn't hold up shutdown while waiting on a long poll.
        threading.Thread(target=self.run, name="disruptions", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
