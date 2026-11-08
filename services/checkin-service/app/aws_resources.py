"""Creates the table and queues this service owns (contracts: the sender's owner creates them).

Safe to run repeatedly. Used by infra/create_resources.py and, locally, on startup when
CREATE_RESOURCES=true.
"""
import json
import logging

from botocore.exceptions import ClientError

log = logging.getLogger("checkin.resources")


PENDING_INDEX_SPEC = {
    "IndexName": "pending-index",
    "KeySchema": [
        {"AttributeName": "pending", "KeyType": "HASH"},
        {"AttributeName": "receivedAt", "KeyType": "RANGE"},
    ],
    "Projection": {"ProjectionType": "ALL"},
}

ATTRIBUTES = [
    {"AttributeName": "tripId", "AttributeType": "S"},
    {"AttributeName": "seq", "AttributeType": "N"},
    {"AttributeName": "pending", "AttributeType": "S"},
    {"AttributeName": "receivedAt", "AttributeType": "S"},
]


def ensure_table(dynamodb_client, table_name: str) -> None:
    try:
        dynamodb_client.create_table(
            TableName=table_name,
            AttributeDefinitions=ATTRIBUTES,
            KeySchema=[
                {"AttributeName": "tripId", "KeyType": "HASH"},
                {"AttributeName": "seq", "KeyType": "RANGE"},
            ],
            GlobalSecondaryIndexes=[PENDING_INDEX_SPEC],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb_client.get_waiter("table_exists").wait(TableName=table_name)
        log.info("created table %s", table_name)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceInUseException":
            raise
        # Table from an older version of this service: add the index if it's missing.
        gsis = dynamodb_client.describe_table(TableName=table_name)["Table"].get("GlobalSecondaryIndexes", [])
        if not any(g["IndexName"] == "pending-index" for g in gsis):
            dynamodb_client.update_table(
                TableName=table_name,
                AttributeDefinitions=ATTRIBUTES,
                GlobalSecondaryIndexUpdates=[{"Create": PENDING_INDEX_SPEC}],
            )
            log.info("added pending-index to %s", table_name)
    try:
        dynamodb_client.update_time_to_live(
            TableName=table_name,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": "expiresAt"},
        )
    except ClientError as e:
        # Raised when TTL is already enabled.
        if e.response["Error"]["Code"] != "ValidationException":
            raise


def ensure_queues(sqs_client, queue_name: str) -> str:
    """contracts/queues.md: standard queue, visibility 30 s, long polling 20 s, DLQ after 5 tries."""
    dlq_url = sqs_client.create_queue(QueueName=f"{queue_name}-dlq")["QueueUrl"]
    dlq_arn = sqs_client.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])[
        "Attributes"
    ]["QueueArn"]
    attrs = {
        "VisibilityTimeout": "30",
        "ReceiveMessageWaitTimeSeconds": "20",
        "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "5"}),
    }
    url = sqs_client.create_queue(QueueName=queue_name)["QueueUrl"]
    sqs_client.set_queue_attributes(QueueUrl=url, Attributes=attrs)
    log.info("queue ready %s (DLQ %s)", url, dlq_url)
    return url
