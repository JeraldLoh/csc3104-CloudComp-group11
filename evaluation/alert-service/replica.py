"""One Alert Service replica for the experiments: the real service (services/alert-service/app), changed from
outside in two ways, so the service's own code stays exactly as deployed.

- Pushes go to a recorder instead of Firebase. Each one is appended to the Redis list `eval:pushes` with the time
  and the replica that sent it, so run.py can count them. Thousands of real pushes would prove nothing more.
- With EVAL_WRITES=naive, the conditional writes the design depends on are done the naive way instead: read the
  item, decide in Python, then write without a condition (ReadThenWrite). This is the version the experiments
  compare against.
- With EVAL_OUTBOX=off, pushes are sent as before the push outbox (services/alert-service/app/push.py): straight
  away, without claiming them on the incident, and a push a replica didn't send is never sent by another.

    python evaluation/alert-service/replica.py --port 18101 --name r1

Every other setting comes from the environment, as for the service itself (run.py sets them).
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from botocore.exceptions import ClientError

SERVICE = Path(__file__).resolve().parents[2] / "services" / "alert-service"
sys.path.insert(0, str(SERVICE))

PUSHES_KEY = "eval:pushes"


class RecordingSender:
    """Stands in for push.FirebaseSender: records the push and says every browser accepted it."""

    def __init__(self, redis_client, replica: str):
        self.redis = redis_client
        self.replica = replica

    def send(self, tokens: list[str], data: dict[str, str]) -> tuple[int, list[str]]:
        self.redis.rpush(PUSHES_KEY, json.dumps({
            "t": time.time(), "replica": self.replica, "kind": data.get("kind"),
            "incidentId": data.get("incidentId"), "title": data.get("title"),
        }))
        return len(tokens), []


def _condition_failed(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "read-then-write check"}},
                       operation)


class ReadThenWrite:
    """A DynamoDB client whose two conditional writes on the incidents table are done without a condition:
    it reads the item, checks the same rule in Python, then writes. Between the read and the write another request
    can do the same, which is the race a conditional write closes. Every other call passes straight through.

    - creating an incident (`attribute_not_exists(incidentId)`, IncidentStore.record_problem)
    - claiming an escalation level (`#state = :open AND escalationLevel = :seen`, IncidentStore.claim_escalation)
    - claiming a pending push (`pushPending = :pending AND pushLease = :seenLease`, IncidentStore.claim_push)
    """

    def __init__(self, db, table: str):
        self._db = db
        self._table = table

    def __getattr__(self, name):
        return getattr(self._db, name)

    def _read(self, key: dict) -> dict | None:
        return self._db.get_item(TableName=self._table, Key=key, ConsistentRead=True).get("Item")

    def put_item(self, **kw):
        if kw.get("TableName") == self._table and kw.get("ConditionExpression") == "attribute_not_exists(incidentId)":
            if self._read({"incidentId": kw["Item"]["incidentId"]}) is not None:
                raise _condition_failed("PutItem")
            kw.pop("ConditionExpression")
        return self._db.put_item(**kw)

    def update_item(self, **kw):
        condition = kw.get("ConditionExpression", "") if kw.get("TableName") == self._table else ""
        if "escalationLevel = :seen" in condition:
            check, own = (lambda item, v: item.get("state") == v[":open"] and item.get("escalationLevel") == v[":seen"],
                          (":open", ":seen"))
        elif "pushLease = :seenLease" in condition:
            check, own = (lambda item, v: item.get("pushPending") == v[":pending"] and item.get("pushLease") == v[":seenLease"],
                          (":pending", ":seenLease"))
        else:
            return self._db.update_item(**kw)
        values = dict(kw["ExpressionAttributeValues"])
        if not check(self._read(kw["Key"]) or {}, values):
            raise _condition_failed("UpdateItem")
        # DynamoDB refuses names and values the expressions don't use, so drop the condition's own
        kw.pop("ConditionExpression")
        kw.pop("ExpressionAttributeNames", None)
        update = kw["UpdateExpression"]
        kw["ExpressionAttributeValues"] = {k: v for k, v in values.items() if k not in own or k in update}
        return self._db.update_item(**kw)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()

    import uvicorn

    import app.main as service
    from app.config import settings

    service.notifier.sender = RecordingSender(service.r, args.name)
    writes = os.getenv("EVAL_WRITES", "conditional")
    if writes == "naive":
        service.incidents.db = ReadThenWrite(service.dynamodb, settings.incidents_table)
    elif writes != "conditional":
        raise SystemExit(f"EVAL_WRITES must be conditional or naive, not {writes}")
    outbox = os.getenv("EVAL_OUTBOX", "on")
    if outbox == "off":
        # The design before the push outbox: push straight away, never claimed, marked or sent again
        service.notifier.incidents = None
        service.push_retry.start = lambda: None
    elif outbox != "on":
        raise SystemExit(f"EVAL_OUTBOX must be on or off, not {outbox}")
    service.log.info("evaluation replica %s: %s writes, outbox %s, pushes recorded in Redis", args.name, writes, outbox)
    uvicorn.run(service.app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
