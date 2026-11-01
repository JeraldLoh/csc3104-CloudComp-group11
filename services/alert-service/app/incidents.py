"""Incidents in DynamoDB: one per problem, however many times it's reported.

Incident ID = travellerId#tripId#problemType#stepIndex (contracts/dynamo-db-tables.md). The first
report creates the incident with a conditional write (attribute_not_exists), so when several
replicas, retries or re-delivered messages handle the same problem at once, exactly one write
succeeds and the others update that incident instead of creating a duplicate.

States: open -> acknowledged -> resolved. Each change is a conditional update too, so two
caregivers (or two replicas) acting at the same time can't both win.
"""
import time
from datetime import datetime, timezone
from decimal import Decimal

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from app.config import settings
from app.errors import ApiError
from app.models import ProblemReport

# "unknown": detectors agree something is wrong but not what, so the caregiver should check.
URGENT_PROBLEMS = {"no_response", "help_requested", "unknown"}
RESOLVED_TTL_S = 30 * 24 * 3600  # contracts: incidents are deleted 30 days after being resolved
TRAVELLER_INDEX = "travellerId-createdAt-index"
UNKNOWN_STEP = "unknown"  # incident ID's last part when a report's stepIndex is null

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def iso(dt: datetime) -> str:
    """contracts/README.md: ISO 8601 in UTC, ending in Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(datetime.now(timezone.utc))


def incident_id(traveller_id: str, trip_id: str, problem_type: str, step: int | str) -> str:
    return f"{traveller_id}#{trip_id}#{problem_type}#{step}"


def _to_dynamo(value) -> dict:
    """Python value -> DynamoDB attribute. boto3 rejects floats, so they go in as Decimal."""
    if isinstance(value, float):
        return _serializer.serialize(Decimal(str(value)))
    if isinstance(value, dict):
        return {"M": {k: _to_dynamo(v) for k, v in value.items()}}
    if isinstance(value, list):
        return {"L": [_to_dynamo(v) for v in value]}
    return _serializer.serialize(value)


def _plain(value):
    """DynamoDB numbers come back as Decimal; turn them into int/float so they serialise to JSON."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _from_dynamo(item: dict) -> dict:
    return _plain({k: _deserializer.deserialize(v) for k, v in item.items()})


def _key(iid: str) -> dict:
    return {"incidentId": {"S": iid}}


def _set_clause(values: dict) -> tuple[str, dict, dict]:
    """SET expression for `values`, with every name aliased (`state` is a DynamoDB reserved word)."""
    expr = "SET " + ", ".join(f"#{k} = :{k}" for k in values)
    names = {f"#{k}": k for k in values}
    vals = {f":{k}": _to_dynamo(v) for k, v in values.items()}
    return expr, names, vals


def _condition_failed(e: ClientError) -> bool:
    return e.response["Error"]["Code"] == "ConditionalCheckFailedException"


class IncidentStore:
    def __init__(self, dynamodb_client, table_name: str | None = None):
        self.db = dynamodb_client
        self.table = table_name or settings.incidents_table

    # ------------------------------------------------------------------ reports
    def record_problem(self, report: ProblemReport, details: dict | None = None) -> tuple[dict, str]:
        """Create or update the incident for this problem.

        `details`: extra fields to keep on the incident (e.g. {"disruption": {...}}); the latest report's win.
        Returns (incident, outcome); outcome is "created", "updated" or "reopened".
        """
        details = details or {}
        # A missing step must not lose the alert; repeats with no step still share one incident.
        step = UNKNOWN_STEP if report.stepIndex is None else report.stepIndex
        iid = incident_id(report.travellerId, report.tripId, report.problemType, step)
        now = now_iso()
        detected_at = iso(report.timestamp)
        loc = report.location
        location = loc.model_dump() if loc and loc.lat is not None and loc.lng is not None else None

        incident = {
            "incidentId": iid,
            "travellerId": report.travellerId,
            "tripId": report.tripId,
            "stepIndex": report.stepIndex,
            "problemType": report.problemType,
            "severity": "urgent" if report.problemType in URGENT_PROBLEMS else "normal",
            "state": "open",
            "escalationLevel": 0,
            "detectionCount": 1,
            "confidence": report.confidence,
            "lastLocation": location,
            "batteryPct": report.batteryPct,
            "notifiedChannels": [],
            "acknowledgedBy": None,
            "source": report.source,
            "lastReportId": report.reportId,
            # detectedAt -> createdAt is how long the alert took to reach this service (issue 5.6).
            "detectedAt": detected_at,
            "lastDetectedAt": detected_at,
            "createdAt": now,
            "updatedAt": now,
            **details,
        }
        try:
            self.db.put_item(
                TableName=self.table,
                Item={k: _to_dynamo(v) for k, v in incident.items()},
                ConditionExpression="attribute_not_exists(incidentId)",
            )
            return incident, "created"
        except ClientError as e:
            if not _condition_failed(e):
                raise

        # It already exists: a repeat detection, a retry or a re-delivered message.
        expr, names, vals = _set_clause({
            "updatedAt": now,
            "lastDetectedAt": detected_at,
            # A report with no GPS fix must not wipe the last location the caregiver can see.
            **({"lastLocation": location} if location else {}),
            "batteryPct": report.batteryPct,
            "confidence": report.confidence,
            "lastReportId": report.reportId,
            **details,
        })
        names["#detectionCount"] = "detectionCount"
        vals[":one"] = {"N": "1"}
        attrs = self.db.update_item(
            TableName=self.table,
            Key=_key(iid),
            UpdateExpression=expr + ", #detectionCount = #detectionCount + :one",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=vals,
            ReturnValues="ALL_NEW",
        )["Attributes"]
        current = _from_dynamo(attrs)

        # The problem came back after the caregiver resolved it: open the incident again so it isn't
        # missed. Reports from before the resolve (late retries, re-delivered messages) don't count.
        if current["state"] == "resolved" and detected_at > current.get("resolvedAt", ""):
            reopened = self._reopen(iid, current["resolvedAt"], now)
            if reopened:
                return reopened, "reopened"
            return self.get(iid), "updated"  # another replica reopened it first
        return current, "updated"

    def _reopen(self, iid: str, seen_resolved_at: str, now: str) -> dict | None:
        expr, names, vals = _set_clause({
            "state": "open", "escalationLevel": 0, "acknowledgedBy": None, "updatedAt": now, "reopenedAt": now,
        })
        for name in ("resolvedAt", "resolvedBy", "acknowledgedAt", "expiresAt"):
            names[f"#{name}"] = name
        vals[":resolved"] = _to_dynamo("resolved")
        vals[":seenResolvedAt"] = _to_dynamo(seen_resolved_at)
        try:
            attrs = self.db.update_item(
                TableName=self.table,
                Key=_key(iid),
                UpdateExpression=expr + " REMOVE #resolvedAt, #resolvedBy, #acknowledgedAt, #expiresAt",
                ConditionExpression="#state = :resolved AND #resolvedAt = :seenResolvedAt",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=vals,
                ReturnValues="ALL_NEW",
            )["Attributes"]
            return _from_dynamo(attrs)
        except ClientError as e:
            if not _condition_failed(e):
                raise
            return None

    # ------------------------------------------------------------------ reads
    def get(self, iid: str) -> dict:
        item = self.db.get_item(TableName=self.table, Key=_key(iid), ConsistentRead=True).get("Item")
        if not item:
            raise ApiError(404, "not_found", f"Incident {iid} not found")
        return _from_dynamo(item)

    def list_for_traveller(self, traveller_id: str, limit: int = 50) -> list[dict]:
        """Newest first."""
        resp = self.db.query(
            TableName=self.table,
            IndexName=TRAVELLER_INDEX,
            KeyConditionExpression="travellerId = :t",
            ExpressionAttributeValues={":t": {"S": traveller_id}},
            ScanIndexForward=False,
            Limit=limit,
        )
        return [_from_dynamo(item) for item in resp["Items"]]

    # ------------------------------------------------------------------ state changes
    def acknowledge(self, iid: str, user_id: str | None) -> dict:
        now = now_iso()
        updated = self._transition(
            iid, {"state": "acknowledged", "acknowledgedBy": user_id, "acknowledgedAt": now, "updatedAt": now},
            allowed_from=("open",),
        )
        if updated:
            return updated
        current = self.get(iid)  # 404 if it doesn't exist
        if current["state"] == "acknowledged":
            return current  # already done (e.g. the dashboard retried): same answer again
        raise ApiError(409, "conflict", f"Incident {iid} is already {current['state']}")

    def resolve(self, iid: str, user_id: str | None) -> dict:
        now = now_iso()
        updated = self._transition(
            iid,
            {"state": "resolved", "resolvedBy": user_id, "resolvedAt": now, "updatedAt": now,
             "expiresAt": int(time.time()) + RESOLVED_TTL_S},
            allowed_from=("open", "acknowledged"),
        )
        if updated:
            return updated
        return self.get(iid)  # 404 if it doesn't exist; otherwise it was already resolved

    def _transition(self, iid: str, values: dict, allowed_from: tuple[str, ...]) -> dict | None:
        """Apply `values` only if the incident exists and its state is in `allowed_from`.

        Returns the updated incident, or None if the condition didn't hold.
        """
        expr, names, vals = _set_clause(values)
        names["#state"] = "state"
        placeholders = []
        for i, state in enumerate(allowed_from):
            vals[f":from{i}"] = _to_dynamo(state)
            placeholders.append(f":from{i}")
        try:
            attrs = self.db.update_item(
                TableName=self.table,
                Key=_key(iid),
                UpdateExpression=expr,
                ConditionExpression=f"attribute_exists(incidentId) AND #state IN ({', '.join(placeholders)})",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=vals,
                ReturnValues="ALL_NEW",
            )["Attributes"]
            return _from_dynamo(attrs)
        except ClientError as e:
            if not _condition_failed(e):
                raise
            return None
