"""Profiles (issue 5.1): travellers, which caregivers look after them, and their safe locations.

Tables (contracts/dynamo-db-tables.md): travellers (travellerId), caregiver-links (caregiverId + travellerId),
safe-locations (travellerId + locationId).

caregiver-links is the access rule for the whole service: a caregiver sees a traveller's profile, safe
locations and alerts only if a link exists. A link is made when a caregiver creates the traveller, or
by a caregiver who is already linked (add_link), so nobody can attach themselves to a stranger.
"""
import secrets

from botocore.exceptions import ClientError

from app.config import settings
from app.errors import ApiError
from app.incidents import _from_dynamo, _to_dynamo, now_iso


def _item(values: dict) -> dict:
    return {k: _to_dynamo(v) for k, v in values.items()}


class ProfileStore:
    def __init__(self, dynamodb_client):
        self.db = dynamodb_client
        self.travellers = settings.travellers_table
        self.links = settings.caregiver_links_table
        self.locations = settings.safe_locations_table

    # ------------------------------------------------------------------ travellers
    def create_traveller(self, caregiver_id: str, name: str, preferences: dict) -> dict:
        """A new traveller with a fresh U- ID, linked to the caregiver who created them.
        Both items are written in one transaction: never a traveller nobody can see."""
        now = now_iso()
        for _ in range(5):  # IDs are random; a clash is vanishingly rare
            traveller = {"travellerId": "U-" + secrets.token_hex(3).upper(), "name": name,
                         "preferences": preferences, "createdBy": caregiver_id, "createdAt": now}
            link = self._link(caregiver_id, traveller["travellerId"], caregiver_id, now)
            try:
                self.db.transact_write_items(TransactItems=[
                    {"Put": {"TableName": self.travellers, "Item": _item(traveller),
                             "ConditionExpression": "attribute_not_exists(travellerId)"}},
                    {"Put": {"TableName": self.links, "Item": _item(link)}},
                ])
                return traveller
            except ClientError as e:
                if e.response["Error"]["Code"] != "TransactionCanceledException":
                    raise
        raise RuntimeError("couldn't find a free traveller ID")

    def get_traveller(self, traveller_id: str) -> dict | None:
        item = self.db.get_item(TableName=self.travellers, Key={"travellerId": {"S": traveller_id}}).get("Item")
        return _from_dynamo(item) if item else None

    def linked_travellers(self, caregiver_id: str) -> list[dict]:
        """The caregiver's travellers, by name."""
        ids = self.linked_traveller_ids(caregiver_id)
        found: list[dict] = []
        for start in range(0, len(ids), 100):  # BatchGetItem takes at most 100 keys
            request = {self.travellers: {"Keys": [{"travellerId": {"S": i}} for i in ids[start:start + 100]]}}
            while request:
                resp = self.db.batch_get_item(RequestItems=request)
                found += [_from_dynamo(item) for item in resp["Responses"].get(self.travellers, [])]
                request = resp.get("UnprocessedKeys") or None
        return sorted(found, key=lambda t: t.get("name", "").lower())

    # ------------------------------------------------------------------ links
    def is_linked(self, caregiver_id: str, traveller_id: str) -> bool:
        key = {"caregiverId": {"S": caregiver_id}, "travellerId": {"S": traveller_id}}
        return "Item" in self.db.get_item(TableName=self.links, Key=key)

    def linked_traveller_ids(self, caregiver_id: str) -> list[str]:
        ids: list[str] = []
        kwargs = {"TableName": self.links, "KeyConditionExpression": "caregiverId = :g",
                  "ExpressionAttributeValues": {":g": {"S": caregiver_id}}, "ProjectionExpression": "travellerId"}
        while True:
            resp = self.db.query(**kwargs)
            ids += [item["travellerId"]["S"] for item in resp["Items"]]
            if "LastEvaluatedKey" not in resp:
                return ids
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def add_link(self, by_caregiver: str, traveller_id: str, caregiver_id: str) -> dict:
        """`by_caregiver` adds `caregiver_id` to the traveller. Safe to repeat: an existing link is returned."""
        if not self.is_linked(by_caregiver, traveller_id):
            raise ApiError(404, "not_found", f"Traveller {traveller_id} not found")
        link = self._link(caregiver_id, traveller_id, by_caregiver, now_iso())
        try:
            self.db.put_item(TableName=self.links, Item=_item(link),
                             ConditionExpression="attribute_not_exists(caregiverId)")
            return link
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        key = {"caregiverId": {"S": caregiver_id}, "travellerId": {"S": traveller_id}}
        return _from_dynamo(self.db.get_item(TableName=self.links, Key=key, ConsistentRead=True)["Item"])

    def caregivers_of(self, traveller_id: str) -> list[str]:
        """Who looks after this traveller (the table's travellerId-index)."""
        resp = self.db.query(TableName=self.links, IndexName="travellerId-index",
                             KeyConditionExpression="travellerId = :t",
                             ExpressionAttributeValues={":t": {"S": traveller_id}})
        return [item["caregiverId"]["S"] for item in resp["Items"]]

    def remove_traveller(self, caregiver_id: str, traveller_id: str) -> bool:
        """The caregiver stops looking after the traveller (their link goes). If nobody else looks after them,
        their profile and safe locations are deleted too. Returns True if the profile was deleted.
        Their past incidents stay, out of sight, until they expire."""
        if not self.is_linked(caregiver_id, traveller_id):
            raise ApiError(404, "not_found", f"Traveller {traveller_id} not found")
        self.db.delete_item(TableName=self.links,
                            Key={"caregiverId": {"S": caregiver_id}, "travellerId": {"S": traveller_id}})
        if self.caregivers_of(traveller_id):
            return False
        for location in self.safe_locations(traveller_id):
            self.db.delete_item(TableName=self.locations, Key={"travellerId": {"S": traveller_id},
                                                               "locationId": {"S": location["locationId"]}})
        self.db.delete_item(TableName=self.travellers, Key={"travellerId": {"S": traveller_id}})
        return True

    @staticmethod
    def _link(caregiver_id: str, traveller_id: str, linked_by: str, now: str) -> dict:
        return {"caregiverId": caregiver_id, "travellerId": traveller_id, "linkedBy": linked_by, "linkedAt": now}

    # ------------------------------------------------------------------ safe locations
    def safe_locations(self, traveller_id: str) -> list[dict]:
        resp = self.db.query(TableName=self.locations, KeyConditionExpression="travellerId = :t",
                             ExpressionAttributeValues={":t": {"S": traveller_id}})
        return sorted((_from_dynamo(i) for i in resp["Items"]), key=lambda loc: loc["name"].lower())

    def add_safe_location(self, traveller_id: str, name: str, lat: float, lng: float, added_by: str | None) -> dict:
        location = {"travellerId": traveller_id, "locationId": "L-" + secrets.token_hex(4).upper(),
                    "name": name, "lat": lat, "lng": lng, "addedBy": added_by, "createdAt": now_iso()}
        self.db.put_item(TableName=self.locations, Item=_item(location))
        return location
