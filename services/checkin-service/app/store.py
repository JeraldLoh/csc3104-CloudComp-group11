"""DynamoDB table careroute-checkins: partition key tripId, sort key seq.

The table is the record of truth for "has this check-in been seen before?".
A conditional put decides it atomically, so duplicates are caught even when two
replicas receive the same retry at the same moment, and even when a batch delivers
older seq numbers after newer ones (which a "highest seq" check would wrongly reject).
"""
import json
import time
from dataclasses import dataclass
from decimal import Decimal

from botocore.exceptions import ClientError

from .models import Checkin


@dataclass
class PutResult:
    created: bool          # False = duplicate (same tripId + seq already stored)
    enqueued: bool         # for duplicates: had the first attempt already reached SQS?


class CheckinStore:
    def __init__(self, dynamodb_resource, table_name: str, ttl_days: int):
        self.table = dynamodb_resource.Table(table_name)
        self.ttl_s = ttl_days * 86400

    def put_new(self, checkin: Checkin, received_at: str) -> PutResult:
        # DynamoDB needs Decimal, not float.
        item = json.loads(json.dumps(checkin.to_contract()), parse_float=Decimal)
        item.update(
            receivedAt=received_at,
            # TTL from arrival time, not the phone's timestamp, so a wrong phone
            # clock can't make a check-in expire early or never.
            expiresAt=int(time.time()) + self.ttl_s,
            enqueued=False,
        )
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(seq)")
            return PutResult(created=True, enqueued=False)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        existing = self.table.get_item(
            Key={"tripId": checkin.tripId, "seq": checkin.seq}, ConsistentRead=True
        ).get("Item", {})
        return PutResult(created=False, enqueued=bool(existing.get("enqueued", True)))

    def mark_enqueued(self, trip_id: str, seq: int) -> None:
        self.table.update_item(
            Key={"tripId": trip_id, "seq": seq},
            UpdateExpression="SET enqueued = :t",
            ExpressionAttributeValues={":t": True},
        )

    def ping(self) -> None:
        self.table.load()
