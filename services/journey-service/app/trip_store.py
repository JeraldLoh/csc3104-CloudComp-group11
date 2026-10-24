"""Where trips live: the same two backends as routes (see route_store.py).

    create(trip)                              -> TripExists if the id is taken
    get(trip_id)                              -> dict or None
    replace(trip, expected_status)            -> TripNotFound / TripConflict; atomic compare-and-set on status
    list_for_traveller(traveller_id, date)    -> list of trips (date optional, "2026-10-12")

DynamoTripStore is the table `careroute-trips` from contracts/dynamo-db-tables.md: PK tripId, indexes on
travellerId + date and on status, and a TTL on `expiresAt` (30 days after the trip). `expiresAt` is stored
but never returned by the API; the stores keep it, JourneyClient strips it.
"""
import json

from .route_store import _from_dynamo, _load, _to_dynamo


class TripError(Exception):
    pass


class TripNotFound(TripError):
    pass


class TripExists(TripError):
    pass


class TripConflict(TripError):
    """The request can't be done in the trip's current state (or someone changed it first)."""

    def __init__(self, message: str = "The trip was changed by someone else. Reload it and try again."):
        super().__init__(message)
        self.message = message


class RedisTripStore:
    def __init__(self, redis_client, prefix: str = "journey:trip"):
        self._r = redis_client
        self._prefix = prefix

    def _key(self, trip_id: str) -> str:
        return f"{self._prefix}:{trip_id}"

    def _index(self, traveller_id: str) -> str:
        return f"{self._prefix}s:traveller:{traveller_id}"

    def create(self, trip: dict) -> None:
        if not self._r.set(self._key(trip["tripId"]), json.dumps(trip), nx=True):
            raise TripExists(trip["tripId"])
        self._r.sadd(self._index(trip["travellerId"]), trip["tripId"])

    def get(self, trip_id: str) -> dict | None:
        raw = self._r.get(self._key(trip_id))
        return None if raw is None else _load(raw)

    def replace(self, trip: dict, expected_status: str) -> None:
        import redis  # local import: only this store needs the exception type

        key = self._key(trip["tripId"])
        with self._r.pipeline() as pipe:
            pipe.watch(key)
            raw = pipe.get(key)
            if raw is None:
                pipe.unwatch()
                raise TripNotFound(trip["tripId"])
            if _load(raw)["status"] != expected_status:
                pipe.unwatch()
                raise TripConflict()
            pipe.multi()
            pipe.set(key, json.dumps(trip))
            try:
                pipe.execute()
            except redis.WatchError:
                raise TripConflict() from None

    def list_for_traveller(self, traveller_id: str, date: str | None = None) -> list[dict]:
        ids = sorted(i.decode() if isinstance(i, bytes) else i for i in self._r.smembers(self._index(traveller_id)))
        trips = [t for t in (self.get(i) for i in ids) if t is not None]
        return [t for t in trips if date is None or t["date"] == date]


class DynamoTripStore:
    def __init__(self, dynamodb_resource, table_name: str = "careroute-trips",
                 traveller_index: str = "travellerId-date-index"):
        self._table = dynamodb_resource.Table(table_name)
        self._index = traveller_index

    def ping(self) -> None:
        """Raises if the table can't be read; a GetItem on a key that doesn't exist (see DynamoRouteStore.ping)."""
        self._table.get_item(Key={"tripId": "T-000000"})

    @staticmethod
    def _failed_condition(error) -> bool:
        return error.response["Error"]["Code"] == "ConditionalCheckFailedException"

    def create(self, trip: dict) -> None:
        from botocore.exceptions import ClientError
        try:
            self._table.put_item(Item=_to_dynamo(trip), ConditionExpression="attribute_not_exists(tripId)")
        except ClientError as e:
            if self._failed_condition(e):
                raise TripExists(trip["tripId"]) from None
            raise

    def get(self, trip_id: str) -> dict | None:
        item = self._table.get_item(Key={"tripId": trip_id}, ConsistentRead=True).get("Item")
        return None if item is None else _from_dynamo(item)

    def replace(self, trip: dict, expected_status: str) -> None:
        """One conditional write; `status` is a reserved word in DynamoDB expressions, hence the #s alias."""
        from botocore.exceptions import ClientError
        try:
            self._table.put_item(
                Item=_to_dynamo(trip),
                ConditionExpression="#s = :expected",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":expected": expected_status},
            )
        except ClientError as e:
            if not self._failed_condition(e):
                raise
            if self.get(trip["tripId"]) is None:
                raise TripNotFound(trip["tripId"]) from None
            raise TripConflict() from None

    def list_for_traveller(self, traveller_id: str, date: str | None = None) -> list[dict]:
        from boto3.dynamodb.conditions import Key
        condition = Key("travellerId").eq(traveller_id)
        if date is not None:
            condition = condition & Key("date").eq(date)
        trips, start = [], None
        while True:
            page = self._table.query(IndexName=self._index, KeyConditionExpression=condition,
                                     **({"ExclusiveStartKey": start} if start else {}))
            trips += [_from_dynamo(i) for i in page["Items"]]
            start = page.get("LastEvaluatedKey")
            if not start:
                return trips
