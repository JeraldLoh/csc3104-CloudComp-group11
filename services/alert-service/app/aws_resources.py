"""Creates this service's DynamoDB tables and the disruptions queue in LocalStack
(CREATE_RESOURCES=true, local only).

On AWS these come from the platform stack (cloudformation/careroute-vpc-eks-standard.yaml);
the keys, indexes and queue settings here must match it. Safe to run repeatedly.
"""
import json
import logging

from botocore.exceptions import ClientError

from app.config import settings

log = logging.getLogger("alert.resources")


def _s(name: str) -> dict:
    return {"AttributeName": name, "AttributeType": "S"}


def _key(hash_key: str, range_key: str | None = None) -> list[dict]:
    key = [{"AttributeName": hash_key, "KeyType": "HASH"}]
    if range_key:
        key.append({"AttributeName": range_key, "KeyType": "RANGE"})
    return key


def _index(name: str, hash_key: str, range_key: str | None = None) -> dict:
    return {"IndexName": name, "KeySchema": _key(hash_key, range_key), "Projection": {"ProjectionType": "ALL"}}


def table_specs() -> list[dict]:
    """contracts/dynamo-db-tables.md, Member 5 rows."""
    return [
        {
            "TableName": settings.incidents_table,
            "AttributeDefinitions": [_s("incidentId"), _s("travellerId"), _s("createdAt")],
            "KeySchema": _key("incidentId"),
            "GlobalSecondaryIndexes": [_index("travellerId-createdAt-index", "travellerId", "createdAt")],
            "ttl": "expiresAt",
        },
        {
            "TableName": settings.travellers_table,
            "AttributeDefinitions": [_s("travellerId")],
            "KeySchema": _key("travellerId"),
        },
        {
            "TableName": settings.caregiver_links_table,
            "AttributeDefinitions": [_s("caregiverId"), _s("travellerId")],
            "KeySchema": _key("caregiverId", "travellerId"),
            "GlobalSecondaryIndexes": [_index("travellerId-index", "travellerId")],
        },
        {
            "TableName": settings.safe_locations_table,
            "AttributeDefinitions": [_s("travellerId"), _s("locationId")],
            "KeySchema": _key("travellerId", "locationId"),
        },
        {
            "TableName": settings.device_tokens_table,
            "AttributeDefinitions": [_s("userId"), _s("token")],
            "KeySchema": _key("userId", "token"),
        },
    ]


def ensure_tables(dynamodb_client) -> None:
    for spec in table_specs():
        spec = dict(spec)
        ttl = spec.pop("ttl", None)
        name = spec["TableName"]
        try:
            dynamodb_client.create_table(BillingMode="PAY_PER_REQUEST", **spec)
            dynamodb_client.get_waiter("table_exists").wait(TableName=name)
            log.info("created table %s", name)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceInUseException":
                raise
        if ttl:
            try:
                dynamodb_client.update_time_to_live(
                    TableName=name, TimeToLiveSpecification={"Enabled": True, "AttributeName": ttl}
                )
            except ClientError as e:
                # Raised when TTL is already enabled.
                if e.response["Error"]["Code"] != "ValidationException":
                    raise


def ensure_queue(sqs_client, queue_name: str, visibility_timeout_s: int = 60) -> str:
    """contracts/queues.md (careroute-disruptions): standard queue, visibility 60 s, long polling 20 s,
    after 5 failed attempts a message moves to <queue_name>-dlq. Returns the queue URL."""
    dlq_url = sqs_client.create_queue(QueueName=f"{queue_name}-dlq")["QueueUrl"]
    dlq_arn = sqs_client.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    url = sqs_client.create_queue(QueueName=queue_name)["QueueUrl"]
    sqs_client.set_queue_attributes(QueueUrl=url, Attributes={
        "VisibilityTimeout": str(visibility_timeout_s),
        "ReceiveMessageWaitTimeSeconds": "20",
        "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "5"}),
    })
    log.info("queue ready %s", url)
    return url
