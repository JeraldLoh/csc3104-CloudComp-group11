"""Create the careroute-checkins table and queues (with DLQ). Safe to run more than once.

LocalStack:  AWS_ENDPOINT_URL=http://localhost:4566 python -m infra.create_resources
Real AWS:    python -m infra.create_resources        (uses your normal AWS credentials)
"""
import logging

import boto3

from app.aws_resources import ensure_queues, ensure_table
from app.config import settings

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    aws = dict(region_name=settings.aws_region, endpoint_url=settings.aws_endpoint_url)
    ensure_table(boto3.client("dynamodb", **aws), settings.table_name)
    print("queue:", ensure_queues(boto3.client("sqs", **aws), settings.queue_name))
