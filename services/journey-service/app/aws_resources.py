# To be on AWS Lambda, the service needs only the files in app/ (plus config.py, cache.py, lta.py, disruption.py and aws_resources.py). The rest is for local development and testing. 
# The service is run on a schedule by EventBridge (every minute) to check for disruptions and send them to the disruptions queue. 
# The disruptions queue is created by this service if CREATE_RESOURCES=true; otherwise it must already exist. 
# The routes table is created by this service if CREATE_RESOURCES=true; the trips table and packs bucket are also created if CREATE_RESOURCES=true.
"""Creates what this service owns in AWS: the routes table (contracts/dynamo-db-tables.md) and the
disruptions queue (contracts/queues.md: the sender's owner creates it).

Safe to run repeatedly. Used locally on startup when CREATE_RESOURCES=true, and as a one-off:

    python -m app.aws_resources          (reads AWS_ENDPOINT_URL etc. like the service does)
"""
import json
import logging

log = logging.getLogger("journey.resources")


def ensure_routes_table(dynamodb_client, table_name: str, index_name: str = "travellerId-index") -> None:
    """Table `careroute-routes`: PK routeId, global index on travellerId, on-demand billing."""
    from botocore.exceptions import ClientError
    try:
        dynamodb_client.create_table(
            TableName=table_name,
            AttributeDefinitions=[
                {"AttributeName": "routeId", "AttributeType": "S"},
                {"AttributeName": "travellerId", "AttributeType": "S"},
            ],
            KeySchema=[{"AttributeName": "routeId", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[{
                "IndexName": index_name,
                "KeySchema": [{"AttributeName": "travellerId", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb_client.get_waiter("table_exists").wait(TableName=table_name)
        log.info("created table %s", table_name)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceInUseException":
            raise


def ensure_trips_table(dynamodb_client, table_name: str) -> None:
    """Table `careroute-trips`: PK tripId; indexes on travellerId + date and on status; TTL on expiresAt."""
    from botocore.exceptions import ClientError
    try:
        dynamodb_client.create_table(
            TableName=table_name,
            AttributeDefinitions=[
                {"AttributeName": "tripId", "AttributeType": "S"},
                {"AttributeName": "travellerId", "AttributeType": "S"},
                {"AttributeName": "date", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
            ],
            KeySchema=[{"AttributeName": "tripId", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[
                {"IndexName": "travellerId-date-index",
                 "KeySchema": [{"AttributeName": "travellerId", "KeyType": "HASH"},
                               {"AttributeName": "date", "KeyType": "RANGE"}],
                 "Projection": {"ProjectionType": "ALL"}},
                {"IndexName": "status-index",
                 "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}],
                 "Projection": {"ProjectionType": "ALL"}},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb_client.get_waiter("table_exists").wait(TableName=table_name)
        log.info("created table %s", table_name)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceInUseException":
            raise
    try:
        dynamodb_client.update_time_to_live(
            TableName=table_name, TimeToLiveSpecification={"Enabled": True, "AttributeName": "expiresAt"})
    except ClientError as e:
        if e.response["Error"]["Code"] != "ValidationException":     # raised when TTL is already enabled
            raise


def ensure_packs_bucket(s3_client, bucket: str, region: str) -> None:
    """Private bucket for route packs. The CORS rule lets the traveller app (a browser) download a pack
    from its presigned link; tighten AllowedOrigins to the app's address on AWS."""
    from botocore.exceptions import ClientError
    try:
        kwargs = {} if region == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": region}}
        s3_client.create_bucket(Bucket=bucket, **kwargs)
        log.info("created bucket %s", bucket)
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise
    s3_client.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": [
        {"AllowedMethods": ["GET"], "AllowedOrigins": ["*"], "AllowedHeaders": ["*"], "MaxAgeSeconds": 600}]})


def ensure_disruptions_queue(sqs_client, queue_name: str) -> str:
    """contracts/queues.md: standard queue, visibility 60 s, DLQ after 5 tries."""
    dlq_url = sqs_client.create_queue(QueueName=f"{queue_name}-dlq")["QueueUrl"]
    dlq_arn = sqs_client.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])[
        "Attributes"
    ]["QueueArn"]
    attrs = {
        "VisibilityTimeout": "60",
        "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "5"}),
    }
    url = sqs_client.create_queue(QueueName=queue_name)["QueueUrl"]
    sqs_client.set_queue_attributes(QueueUrl=url, Attributes=attrs)
    log.info("queue ready %s (DLQ %s)", url, dlq_url)
    return url


if __name__ == "__main__":
    import boto3

    from .config import Settings

    s = Settings()
    aws = dict(region_name=s.aws_region, endpoint_url=s.aws_endpoint_url)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ensure_routes_table(boto3.client("dynamodb", **aws), s.routes_table)
    ensure_trips_table(boto3.client("dynamodb", **aws), s.trips_table)
    ensure_packs_bucket(boto3.client("s3", **aws), s.packs_bucket or "careroute-packs", s.aws_region)
    ensure_disruptions_queue(boto3.client("sqs", **aws), s.disruptions_queue)
