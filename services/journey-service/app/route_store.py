"""Where saved routes live.

Two stores with the same four methods (JourneyClient doesn't care which it gets):

    create(route)                       -> RouteExists if the id is taken
    get(route_id)                       -> dict or None
    replace(route, expected_version)    -> RouteNotFound / RouteConflict; atomic compare-and-set
    list_for_traveller(traveller_id)    -> list of routes

DynamoRouteStore is the real one: the table `careroute-routes` from contracts/dynamo-db-tables.md
(PK routeId, global index on travellerId). It runs against real DynamoDB or LocalStack.
RedisRouteStore is the zero-setup fallback for demos and tests: contracts/redis-keys.md says Redis only
holds data that can be rebuilt, so it isn't durable the way DynamoDB is. Pick with ROUTES_STORE.
"""
import json
from decimal import Decimal


class RouteError(Exception):
    pass


class RouteNotFound(RouteError):
    pass


class RouteExists(RouteError):
    pass


class RouteInvalid(RouteError):
    """The request is well-formed but not allowed, e.g. changing a route's traveller."""


class RouteConflict(RouteError):
    """Someone saved a newer version first."""

    def __init__(self, current_version: int | None = None):
        super().__init__("route was changed by someone else")
        self.current_version = current_version


def _load(raw) -> dict:
    return json.loads(raw)


class RedisRouteStore:
    def __init__(self, redis_client, prefix: str = "journey:route"):
        self._r = redis_client
        self._prefix = prefix

    def _key(self, route_id: str) -> str:
        return f"{self._prefix}:{route_id}"

    def _index(self, traveller_id: str) -> str:
        return f"{self._prefix}s:traveller:{traveller_id}"

    def create(self, route: dict) -> None:
        if not self._r.set(self._key(route["routeId"]), json.dumps(route), nx=True):
            raise RouteExists(route["routeId"])
        self._r.sadd(self._index(route["travellerId"]), route["routeId"])

    def get(self, route_id: str) -> dict | None:
        raw = self._r.get(self._key(route_id))
        return None if raw is None else _load(raw)

    def replace(self, route: dict, expected_version: int) -> None:
        """Writes `route` only if the stored version is still `expected_version`."""
        import redis  # local import: only this store needs the exception type

        key = self._key(route["routeId"])
        with self._r.pipeline() as pipe:
            pipe.watch(key)
            raw = pipe.get(key)
            if raw is None:
                pipe.unwatch()
                raise RouteNotFound(route["routeId"])
            current = _load(raw)
            if current["version"] != expected_version:
                pipe.unwatch()
                raise RouteConflict(current["version"])
            pipe.multi()
            pipe.set(key, json.dumps(route))
            try:
                pipe.execute()
            except redis.WatchError:                 # changed between our read and our write
                latest = self.get(route["routeId"])
                raise RouteConflict(latest["version"] if latest else None) from None

    def list_for_traveller(self, traveller_id: str) -> list[dict]:
        ids = sorted(i.decode() if isinstance(i, bytes) else i for i in self._r.smembers(self._index(traveller_id)))
        routes = [self.get(i) for i in ids]
        return [r for r in routes if r is not None]


# --- DynamoDB ------------------------------------------------------------------------

def _to_dynamo(obj):
    """boto3's resource API rejects floats, so numbers go in as Decimal (exact, no rounding)."""
    return json.loads(json.dumps(obj), parse_float=Decimal)


def _number(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def _from_dynamo(obj):
    """Back to plain JSON types. A whole-number float such as 103.0 comes back as 103; same value in JSON."""
    return json.loads(json.dumps(obj, default=_number))


class DynamoRouteStore:
    def __init__(self, dynamodb_resource, table_name: str = "careroute-routes", index_name: str = "travellerId-index"):
        self._table = dynamodb_resource.Table(table_name)
        self._index = index_name

    def ping(self) -> None:
        """Raises if the table can't be read (used by /ready). Reads one key that doesn't exist rather than calling
        DescribeTable: GetItem is a permission the service needs anyway (the platform's IAM role allows it but not
        DescribeTable), it proves the table exists and the endpoint and permissions work, and it is a data-plane call,
        which suits a probe that runs every few seconds."""
        self._table.get_item(Key={"routeId": "R-000000"})

    @staticmethod
    def _failed_condition(error) -> bool:
        return error.response["Error"]["Code"] == "ConditionalCheckFailedException"

    def create(self, route: dict) -> None:
        from botocore.exceptions import ClientError
        try:
            self._table.put_item(Item=_to_dynamo(route), ConditionExpression="attribute_not_exists(routeId)")
        except ClientError as e:
            if self._failed_condition(e):
                raise RouteExists(route["routeId"]) from None
            raise

    def get(self, route_id: str) -> dict | None:
        item = self._table.get_item(Key={"routeId": route_id}, ConsistentRead=True).get("Item")
        return None if item is None else _from_dynamo(item)

    def replace(self, route: dict, expected_version: int) -> None:
        """One conditional write: DynamoDB applies it only if the stored version is still `expected_version`."""
        from botocore.exceptions import ClientError
        try:
            self._table.put_item(
                Item=_to_dynamo(route),
                ConditionExpression="version = :v",
                ExpressionAttributeValues={":v": expected_version},
            )
        except ClientError as e:
            if not self._failed_condition(e):
                raise
            current = self.get(route["routeId"])
            if current is None:
                raise RouteNotFound(route["routeId"]) from None
            raise RouteConflict(current["version"]) from None

    def list_for_traveller(self, traveller_id: str) -> list[dict]:
        from boto3.dynamodb.conditions import Key
        routes, start = [], None
        while True:
            page = self._table.query(
                IndexName=self._index, KeyConditionExpression=Key("travellerId").eq(traveller_id),
                **({"ExclusiveStartKey": start} if start else {}))
            routes += [_from_dynamo(i) for i in page["Items"]]
            start = page.get("LastEvaluatedKey")
            if not start:
                return routes
