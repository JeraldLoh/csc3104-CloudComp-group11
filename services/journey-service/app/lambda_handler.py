"""AWS Lambda entry point for the disruption checker (handler: app.lambda_handler.handler).

Run it on a schedule with EventBridge, e.g. every minute. Environment: REDIS_URL,
LTA_API_KEY, DISRUPTIONS_QUEUE, AWS_DEFAULT_REGION (and AWS_ENDPOINT_URL locally).

The package needs only this file plus config.py, cache.py, lta.py, disruption.py and aws_resources.py (the
last only for CREATE_RESOURCES=true, local development); not the API code. Third-party: httpx and redis
(boto3 is already in the Lambda runtime).

Local run against docker compose:  python -m app.lambda_handler
"""
import json
import logging

import boto3
import redis

from . import aws_resources
from .config import Settings
from .data_mode import DataMode
from .disruption import DisruptionChecker
from .lta import LtaClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# In Lambda the runtime has already set up the root logger (at WARNING), so basicConfig does nothing there.
logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger("journey.lambda")


def handler(event=None, context=None, s: Settings | None = None) -> dict:
    s = s or Settings()
    r = redis.Redis.from_url(s.redis_url, decode_responses=True)
    sqs = boto3.client("sqs", region_name=s.aws_region, endpoint_url=s.aws_endpoint_url)
    if s.create_resources:
        queue_url = aws_resources.ensure_disruptions_queue(sqs, s.disruptions_queue)
    else:
        queue_url = sqs.get_queue_url(QueueName=s.disruptions_queue)["QueueUrl"]

    # The data mode only switches with DEMO_UI=true (local runs); on AWS it is LTA_MODE, which the stack sets to live.
    checker = DisruptionChecker(r, LtaClient(s, redis_client=r, mode=DataMode(s, r)), s)
    summary = checker.check(send=lambda e: sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(e)))
    log.info("disruption check done: %s", summary)
    return summary


if __name__ == "__main__":
    print(json.dumps(handler(), indent=2))
