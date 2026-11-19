"""The evaluation's own logic: the naive writes it compares against, and how runs are counted. No services needed.

    pytest evaluation/alert-service/tests
"""
import sys
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from replica import ReadThenWrite  # noqa: E402
from run import a1_summary, a2_summary, percentiles  # noqa: E402

TABLE = "incidents"


@pytest.fixture
def db():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ap-southeast-1")
        client.create_table(TableName=TABLE, BillingMode="PAY_PER_REQUEST",
                            AttributeDefinitions=[{"AttributeName": "incidentId", "AttributeType": "S"}],
                            KeySchema=[{"AttributeName": "incidentId", "KeyType": "HASH"}])
        yield client


def create(db, iid: str):
    db.put_item(TableName=TABLE, Item={"incidentId": {"S": iid}, "state": {"S": "open"}, "escalationLevel": {"N": "0"}},
                ConditionExpression="attribute_not_exists(incidentId)")


def claim(db, iid: str, seen: int):
    """As IncidentStore.claim_escalation sends it."""
    return db.update_item(
        TableName=TABLE, Key={"incidentId": {"S": iid}},
        UpdateExpression="SET escalationLevel = :next, lastEscalatedAt = :now, updatedAt = :now",
        ConditionExpression="#state = :open AND escalationLevel = :seen",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={":open": {"S": "open"}, ":seen": {"N": str(seen)}, ":next": {"N": str(seen + 1)},
                                   ":now": {"S": "2026-11-19T00:00:00Z"}})


def test_read_then_write_creates_once_when_nobody_races(db):
    naive = ReadThenWrite(db, TABLE)
    create(naive, "a")
    with pytest.raises(ClientError) as e:
        create(naive, "a")
    assert e.value.response["Error"]["Code"] == "ConditionalCheckFailedException"  # as the service expects


def test_read_then_write_claims_only_the_level_it_read(db):
    naive = ReadThenWrite(db, TABLE)
    create(db, "a")
    claim(naive, "a", 0)
    assert db.get_item(TableName=TABLE, Key={"incidentId": {"S": "a"}})["Item"]["escalationLevel"] == {"N": "1"}
    with pytest.raises(ClientError):
        claim(naive, "a", 0)  # already at 1


def test_read_then_write_leaves_other_calls_alone(db):
    naive = ReadThenWrite(db, TABLE)
    create(db, "a")
    assert naive.get_item(TableName=TABLE, Key={"incidentId": {"S": "a"}})["Item"]["incidentId"] == {"S": "a"}


def test_percentiles_by_nearest_rank():
    assert percentiles(list(range(1, 101))) == {"n": 100, "p50": 50, "p95": 95, "max": 100}
    assert percentiles([]) is None
    assert percentiles([-0.01])["p50"] == 0.0  # not -0.0


def ok(created=False, attempts=1):
    return {"ok": True, "created": created, "attempts": attempts, "ms": 10.0}


def test_a1_counts_against_ground_truth():
    trips = ["T-0", "T-1"]
    rows = [(0, ok(True)), (0, ok(True)), (0, ok()), (1, ok(True)), (1, ok(attempts=3))]
    incidents = [{"tripId": "T-0", "detectionCount": 1}, {"tripId": "T-1", "detectionCount": 3}]
    pushes = [{"kind": "alert", "incidentId": "U-1#T-0#off_route#2", "t": 1.1},
              {"kind": "alert", "incidentId": "U-1#T-0#off_route#2", "t": 1.2}]

    out = a1_summary(rows, trips, {0: 1.0, 1: 1.0}, incidents, pushes)

    assert out["extraCreated"] == 1  # T-0 was "created" twice
    assert out["duplicatePushes"] == 1 and out["missingPushes"] == 1  # T-0 twice, T-1 never
    assert out["lostDetections"] == 2  # T-0: 3 accepted, 1 counted
    assert out["extraDetections"] == 1  # T-1: 2 accepted, 3 counted (a retry after a lost answer)
    assert out["retries"] == 2
    assert out["firstPushMs"]["max"] == pytest.approx(100.0)


def test_a2_counts_extra_and_missing_reminders_and_how_late():
    def push(trip, title, t):
        return {"kind": "alert", "incidentId": f"U-1#{trip}#no_response#2", "title": title, "t": t}

    pushes = [push("T-0", "Urgent: Eval", 0), push("T-0", "Still not answered: Eval", 8.5),
              push("T-0", "Still not answered: Eval", 8.6),  # a duplicate of that level
              push("T-0", "Still not answered: Eval", 17), push("T-1", "Urgent: Eval", 0),
              push("T-1", "Still not answered: Eval", 8)]
    incidents = [{"tripId": "T-0", "escalationLevel": 2}, {"tripId": "T-1", "escalationLevel": 1}]

    out = a2_summary(["T-0", "T-1"], [ok(True), ok(True)], incidents, pushes, after=8, most=2)

    assert out["reminders"] == 4 and out["expectedReminders"] == 4
    assert out["extraReminders"] == 1 and out["alertsWithExtra"] == 1
    assert out["missingReminders"] == 1 and out["alertsWithMissing"] == 1
    assert out["atFinalLevel"] == 1
    assert out["lateS"] == {"n": 3, "p50": 0.5, "p95": 0.5, "max": 0.5}  # T-0: 8.5 - 0 - 8, 17 - 8.5 - 8; T-1: 0
