"""Put contracts/examples/disruption.json on the local disruptions queue, as the Disruption Lambda would.

From services/alert-service, with the LocalStack port-forward running:
    python tools/send_disruption.py               # traveller U-042, as in the example
    python tools/send_disruption.py U-cleavant    # a different traveller
"""
import json
import os
import sys
from pathlib import Path

import boto3

os.environ.setdefault("AWS_ACCESS_KEY_ID", "csc3104")  # LocalStack accepts any value
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "csc3104")

EXAMPLE = Path(__file__).resolve().parents[3] / "contracts" / "examples" / "disruption.json"

if __name__ == "__main__":
    disruption = json.loads(EXAMPLE.read_text())
    if len(sys.argv) > 1:
        disruption["travellerId"] = sys.argv[1]
    sqs = boto3.client(
        "sqs",
        region_name=os.getenv("AWS_DEFAULT_REGION", "ap-southeast-1"),
        endpoint_url=os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566"),
    )
    url = sqs.get_queue_url(QueueName=os.getenv("DISRUPTIONS_QUEUE", "careroute-disruptions"))["QueueUrl"]
    sqs.send_message(QueueUrl=url, MessageBody=json.dumps(disruption))
    print(f"sent {disruption['disruptionId']} for {disruption['travellerId']} to {url}")
