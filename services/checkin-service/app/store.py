"""DynamoDB table careroute-checkins: partition key tripId, sort key seq.

The table is the record of truth for "has this check-in been seen before?".
A conditional put decides it atomically, so duplicates are caught even when two
replicas receive the same retry at the same moment, and even when a batch delivers
older seq numbers after newer ones (which a "highest seq" check would wrongly reject).

Unfinished check-ins carry `pending = "P"` from the moment they're stored until they
have reached the queue. A global secondary index on (pending, receivedAt) contains only
items that have that attribute (a *sparse* index), so the clean-up task can list the
few stuck check-ins with one cheap query, however large the table grows.
"""
import json
import time
from dataclasses import dataclass
from decimal import Decimal

from botocore.exceptions import ClientError

from .models import Checkin

PENDING_INDEX = "pending-index"
PENDING = "P"


@dataclass
class PutResult:
    created: bool          # False = duplicate (same tripId + seq already stored)
    enqueued: bool         # for duplicates: had the first attempt already reached SQS?


def _plain(v):
    """DynamoDB returns numbers as Decimal; turn them back into int/float."""
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v


def item_to_checkin(item: dict) -> Checkin:
    return Checkin.model_validate(_plain(item))


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
            pending=PENDING,
        )
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(seq)")
            return PutResult(created=True, enqueued=False)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        existing = self.get(checkin.tripId, checkin.seq) or {}
        return PutResult(created=False, enqueued=bool(existing.get("enqueued", True)))

    def get(self, trip_id: str, seq: int) -> dict | None:
        return self.table.get_item(Key={"tripId": trip_id, "seq": seq}, ConsistentRead=True).get("Item")

    def mark_enqueued(self, trip_id: str, seq: int) -> None:
        # Removing `pending` takes the item out of the sparse index.
        self.table.update_item(
            Key={"tripId": trip_id, "seq": seq},
            UpdateExpression="SET enqueued = :t REMOVE pending",
            ExpressionAttributeValues={":t": True},
        )

    def find_stuck(self, received_before: str, limit: int) -> list[dict]:
        """Unfinished check-ins stored before `received_before` (ISO time), oldest first.
        The index is eventually consistent, so callers re-read each item before acting."""
        resp = self.table.query(
            IndexName=PENDING_INDEX,
            KeyConditionExpression="pending = :p AND receivedAt < :t",
            ExpressionAttributeValues={":p": PENDING, ":t": received_before},
            Limit=limit,
        )
        return resp.get("Items", [])

    def ping(self) -> None:
        self.table.load()
