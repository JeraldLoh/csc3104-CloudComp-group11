"""Live alert events (alert-event.json) over Redis pub/sub, channel alerts:events (contracts/redis-keys.md).

Whichever replica changes an incident publishes the event; every replica is subscribed and passes it
on to the dashboards connected to it (GET /api/stream, see stream.py). So a caregiver sees every alert,
whichever replica their dashboard is connected to and whichever replica received the problem.
"""
import json
import logging

from app.incidents import now_iso

log = logging.getLogger("alert.events")

CHANNEL = "alerts:events"

# What record_problem's outcome means for the dashboard.
EVENT_FOR_OUTCOME = {"created": "opened", "updated": "updated", "reopened": "reopened"}

MESSAGES = {
    "off_route": "{name} may have gone off route.",
    "wrong_direction": "{name} may be going the wrong way.",
    "missed_stop": "{name} may have missed their stop.",
    "long_dwell": "{name} hasn't moved for a while.",
    "no_response": "{name}'s phone has stopped responding.",
    "help_requested": "{name} has asked for help.",
    "disruption": "{name}'s journey is affected by a service disruption.",
    "unknown": "Something may be wrong on {name}'s journey.",
}


def alert_message(incident: dict, name: str) -> str:
    disruption = incident.get("disruption") or {}
    if disruption.get("message"):
        return f"{name}'s journey is affected: {disruption['message']}."
    return MESSAGES.get(incident["problemType"], MESSAGES["unknown"]).format(name=name)


def build_event(event_type: str, incident: dict, traveller_name: str | None = None) -> dict:
    """alert-event.json. detectionCount and updatedAt are extra (allowed: new optional fields)."""
    name = traveller_name or incident["travellerId"]
    return {
        "event": f"incident.{event_type}",
        "incident": {
            "incidentId": incident["incidentId"],
            "travellerId": incident["travellerId"],
            "travellerName": name,
            "problemType": incident["problemType"],
            "severity": incident["severity"],
            "state": incident["state"],
            "message": alert_message(incident, name),
            "lastLocation": incident.get("lastLocation"),
            "detectionCount": incident.get("detectionCount"),
            "createdAt": incident["createdAt"],
            "updatedAt": incident.get("updatedAt"),
        },
        "sentAt": now_iso(),
    }


class AlertPublisher:
    def __init__(self, redis_client, dynamodb_client=None, travellers_table: str | None = None):
        self.redis = redis_client
        self.db = dynamodb_client
        self.travellers_table = travellers_table
        self._names: dict[str, str] = {}  # names rarely change; saves a read per event

    def publish(self, event_type: str, incident: dict) -> None:
        """Never raises: the incident is already saved, and a dashboard reloads the list when it
        reconnects, so a lost live event only delays what the caregiver sees."""
        try:
            event = build_event(event_type, incident, self._name(incident["travellerId"]))
            self.redis.publish(CHANNEL, json.dumps(event))
        except Exception as e:
            log.warning("couldn't publish %s for %s: %s", event_type, incident.get("incidentId"), e)

    def _name(self, traveller_id: str) -> str | None:
        """From the travellers table (issue 5.1). None until the traveller has a profile."""
        if traveller_id in self._names or not self.db or not self.travellers_table:
            return self._names.get(traveller_id)
        try:
            item = self.db.get_item(TableName=self.travellers_table, Key={"travellerId": {"S": traveller_id}}).get("Item")
        except Exception as e:
            log.warning("couldn't look up the name of %s: %s", traveller_id, e)
            return None
        if item and "name" in item:
            self._names[traveller_id] = item["name"]["S"]
        return self._names.get(traveller_id)
