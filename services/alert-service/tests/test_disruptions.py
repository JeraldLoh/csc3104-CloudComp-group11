"""DisruptionConsumer against a LocalStack queue: each disruption message becomes one incident."""
import json
import uuid

import pytest

from app.aws_resources import ensure_queue
from app.disruptions import DisruptionConsumer
from app.incidents import IncidentStore


@pytest.fixture
def queue_url(sqs):
    """A fresh queue (with its dead-letter queue) per test, so the running service doesn't take our messages."""
    name = f"careroute-test-disruptions-{uuid.uuid4().hex[:8]}"
    url = ensure_queue(sqs, name)
    yield url
    sqs.delete_queue(QueueUrl=url)
    sqs.delete_queue(QueueUrl=sqs.get_queue_url(QueueName=f"{name}-dlq")["QueueUrl"])


@pytest.fixture
def consumer(sqs, queue_url, store):
    return DisruptionConsumer(sqs, queue_url, store, wait_s=1)


def disruption_iid(d: dict) -> str:
    return f"{d['travellerId']}#{d['tripId']}#disruption#{d['affectedStepIndex']}"


def in_flight(sqs, queue_url) -> int:
    """Messages received but not deleted (they reappear after the visibility timeout)."""
    attrs = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["ApproximateNumberOfMessagesNotVisible"])
    return int(attrs["Attributes"]["ApproximateNumberOfMessagesNotVisible"])


def drain(consumer, expected: int) -> int:
    saved = 0
    for _ in range(5):
        saved += consumer.poll_once()
        if saved >= expected:
            break
    return saved


def test_disruption_message_becomes_an_incident_and_is_deleted(sqs, queue_url, consumer, store, disruption):
    sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(disruption))

    assert drain(consumer, 1) == 1

    incident = store.get(disruption_iid(disruption))
    assert incident["state"] == "open"
    assert incident["problemType"] == "disruption"
    assert incident["stepIndex"] == 4
    assert incident["disruption"]["line"] == "NEL"
    assert incident["disruption"]["message"] == "No train service between Punggol and Sengkang"
    # Deleted from the queue: nothing left to receive, nothing waiting to reappear.
    assert sqs.receive_message(QueueUrl=queue_url, WaitTimeSeconds=1).get("Messages", []) == []
    assert in_flight(sqs, queue_url) == 0


def test_same_disruption_delivered_twice_is_one_incident(sqs, queue_url, consumer, store, disruption):
    """Standard queues deliver at least once, so the same message can arrive twice."""
    for _ in range(2):
        sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(disruption))

    assert drain(consumer, 2) == 2

    assert store.get(disruption_iid(disruption))["detectionCount"] == 2
    assert len(store.list_for_traveller(disruption["travellerId"])) == 1


def test_unreadable_message_is_not_deleted(sqs, queue_url, consumer):
    """Left on the queue, so after 5 attempts SQS moves it to the dead-letter queue."""
    sqs.send_message(QueueUrl=queue_url, MessageBody="this is not a disruption")

    assert consumer.poll_once() == 0
    assert in_flight(sqs, queue_url) == 1


def test_message_is_kept_if_the_incident_cannot_be_saved(sqs, queue_url, dynamodb, disruption):
    """E.g. DynamoDB is down: the message must stay on the queue to be retried, not be lost."""
    broken_store = IncidentStore(dynamodb, table_name="careroute-table-that-does-not-exist")
    consumer = DisruptionConsumer(sqs, queue_url, broken_store, wait_s=1)
    sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(disruption))

    assert consumer.poll_once() == 0
    assert in_flight(sqs, queue_url) == 1


def test_queue_sends_repeated_failures_to_the_dead_letter_queue(sqs, queue_url):
    attrs = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["RedrivePolicy", "VisibilityTimeout"])["Attributes"]
    redrive = json.loads(attrs["RedrivePolicy"])

    assert int(redrive["maxReceiveCount"]) == 5
    assert redrive["deadLetterTargetArn"].endswith("-dlq")
    assert attrs["VisibilityTimeout"] == "60"
